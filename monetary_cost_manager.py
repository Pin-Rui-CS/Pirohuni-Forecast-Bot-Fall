from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Final

import httpx

import llm_provider
from utils import _get_field, _json_default

logger = logging.getLogger(__name__)

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
# Re-exported for the call sites that still import it from here. The request
# adaptation (including whether this is sent at all) lives in llm_provider.
OPENROUTER_USAGE_ACCOUNTING: Final[dict[str, Any]] = llm_provider.OPENROUTER_USAGE_ACCOUNTING
# 3.2 chars/token (was 4 until 2026-07-19): the Sonnet-5/Opus-4.7+ tokenizer
# is denser than the old models', and the pipeline's text is URL/table-heavy
# markdown, which tokenizes worse than prose — chars/4 consistently
# under-estimated real billed tokens by ~20-25% (the recurring gap between
# the audit table's implied cost and actual OpenRouter usage). Every budget
# below is denominated in THESE estimated-token units; when this constant
# changes, the budgets must be rescaled by the same factor or every physical
# char budget silently shrinks (they were rescaled ×1.25 with this change).
CHARACTERS_PER_TOKEN = 3.2
# 375K in 3.2-chars units == the 300K chars/4-units limit calibrated on the
# 44773 A/B analysis (raised from 250K there): the measured full pipeline on
# a heavy question (research ~160K + compile ~69K + 3-run ensemble incl. the
# raw-view member ~66K, all in chars/4 units) never fit in the pre-raise
# limit — it forced every heavy question to sacrifice either the compile
# (failed run) or an ensemble member (the "successful" run silently dropped
# its raw-view run at 245K/250K). With the research reserve below, research
# keeps its measured natural appetite while the full tail always fits.
DEFAULT_INPUT_TOKEN_HARD_LIMIT = 375_000
DEFAULT_OUTPUT_TOKEN_HARD_LIMIT = 62_500

# Slice of the per-question input-token budget held back for the mandatory tail
# (compiler precompress + compile + forecaster ensemble), so that optional
# research work (scrape cycles, provider fall-through, artifact retry)
# soft-stops early instead of spending right up to the hard limit and starving
# the calls that produce the deliverable. Calibrated on question 44773's two
# runs (2026-07-16): the failed run spent 230K of 250K on research and the
# compile call was refused; the "successful" rerun finished at 245,126/250,000
# and silently dropped its heterogeneous raw-view ensemble run — refused by
# this same limit pre-flight (forecast.json extra.ensemble: dropped=true, no
# ledger row). Measured tail: compile phase 69,240 (precompress 23,033 +
# 14,403 + Opus compile 31,804), brief-based forecast runs 7,888 each, and the
# raw-view run scales with research size (its prompt ceiling is 200K chars =
# 62.5K estimated tokens; with research gated it runs smaller).
# Values are in 3.2-chars estimated-token units (the chars/4 calibration from
# 44773 — 70K/10K/30K — rescaled ×1.25; same physical char budgets).
# Override with the RESEARCH_RESERVE_INPUT_TOKENS env var.
_RESERVE_BASE_INPUT_TOKENS = 87_500
_RESERVE_PER_FORECAST_RUN_INPUT_TOKENS = 12_500
_RESERVE_HETEROGENEOUS_RUN_EXTRA_INPUT_TOKENS = 37_500


def research_reserve_input_tokens(num_forecast_runs: int) -> int:
    """Input tokens to hold back from research for the compile+forecast tail."""
    override = os.getenv("RESEARCH_RESERVE_INPUT_TOKENS", "").strip()
    if override:
        return max(0, int(override))
    reserve = (
        _RESERVE_BASE_INPUT_TOKENS
        + max(1, num_forecast_runs) * _RESERVE_PER_FORECAST_RUN_INPUT_TOKENS
    )
    # The raw-view heterogeneous member replaces the LAST run when the ensemble
    # has >= 2 runs (forecasters.base.heterogeneous_run_setup); its prompt is
    # the raw research view, far larger than the compiled brief.
    if num_forecast_runs >= 2:
        try:
            from config import HETEROGENEOUS_RUN_ENABLED
        except Exception:
            HETEROGENEOUS_RUN_ENABLED = True
        if HETEROGENEOUS_RUN_ENABLED:
            reserve += _RESERVE_HETEROGENEOUS_RUN_EXTRA_INPUT_TOKENS
    return reserve


class HardLimitExceededError(Exception):
    """Raised when tracked usage would exceed a hard limit."""


@dataclass
class OpenRouterUsageRecord:
    no: int
    name_of_task: str
    input_characters: int
    input_tokens: int
    output_characters: int
    output_tokens: int
    model_used: str
    duration_seconds: float = 0.0
    reasoning_tokens: int = 0
    # OpenRouter-reported ground truth (requires the usage-accounting
    # extra_body, which llm_client always sends). The char-based estimates
    # above stay for continuity; these are the numbers billing actually uses.
    native_input_tokens: int = 0
    native_output_tokens: int = 0
    cached_input_tokens: int = 0
    # Input tokens WRITTEN to the prompt cache. GPT-5.6+ bills these at 1.25x
    # the uncached rate, so a run with writes but no reads is paying a premium
    # for nothing -- worth seeing as its own column rather than buried in cost.
    cache_write_tokens: int = 0
    # OpenRouter: usage.cost (credits) + cost_details.upstream_inference_cost
    # (the provider bill when the key is BYOK, as this project's is).
    # OpenAI: computed from llm_provider.PRICES (no per-request cost is served).
    cost_usd: float = 0.0
    # Which endpoint actually served this call, and how its cost was derived.
    # Without these a $0.000000 row is ambiguous three ways: a free route, a
    # model missing from PRICES, or a call that failed before billing. It is
    # also the only per-row evidence that a call went where it was supposed to
    # -- when runtime fallback lands, a rerouted call shows up here as an
    # endpoint that does not match its route.
    endpoint: str = ""
    cost_source: str = ""       # native | price_table | quota
    # Free in money, not free in budget. SoCLaaS-style gateways meter in
    # microdollars against a daily/monthly allowance, so a run that costs $0
    # can still be stopped dead by quota. Tracked separately from cost_usd so
    # neither can be mistaken for the other.
    quota_microdollars: float = 0.0


class OpenRouterUsageHandle:
    """Mutable handle for one OpenRouter call recorded across active managers."""

    def __init__(
        self,
        records: list[OpenRouterUsageRecord],
        name_of_task: str = "",
        model: str = "",
        route: Any = None,
    ) -> None:
        self._records = records
        self._name_of_task = name_of_task
        self._model = model
        self._route = route
        self._finished = False
        self._started_at = time.monotonic()

    @property
    def input_characters(self) -> int:
        return self._records[0].input_characters if self._records else 0

    @property
    def input_tokens(self) -> int:
        return self._records[0].input_tokens if self._records else 0

    def record_response(self, response: Any) -> None:
        reasoning_tokens = count_openrouter_reasoning_tokens(response)
        native = extract_openrouter_native_usage(
            response, self._model, getattr(self, "_route", None)
        )
        if not self._finished:
            for record in self._records:
                record.reasoning_tokens = reasoning_tokens
                record.native_input_tokens = native["prompt_tokens"]
                record.native_output_tokens = native["completion_tokens"]
                record.cached_input_tokens = native["cached_tokens"]
                record.cache_write_tokens = native["cache_write_tokens"]
                record.cost_usd = native["cost_usd"]
                record.cost_source = native["cost_source"]
                record.quota_microdollars = native["quota_microdollars"]
            logger.info(
                "[usage] %s | model=%s | endpoint=%s | native in/out=%d/%d | "
                "reasoning=%d | cached=%d | cache-write=%d | cost=$%.6f (%s)%s",
                self._name_of_task,
                self._model,
                getattr(getattr(self, "_route", None), "endpoint", None)
                and self._route.endpoint.label or "?",
                native["prompt_tokens"],
                native["completion_tokens"],
                reasoning_tokens,
                native["cached_tokens"],
                native["cache_write_tokens"],
                native["cost_usd"],
                native["cost_source"],
                f" | quota={native['quota_microdollars']:.1f}ud"
                if native["quota_microdollars"] else "",
            )
        self.record_output_characters(count_openrouter_output_characters(response))

    def record_output(self, output: Any) -> None:
        self.record_output_characters(count_serialized_characters(output))

    def record_output_characters(self, output_characters: int) -> None:
        if self._finished:
            return
        if output_characters < 0:
            raise ValueError("output_characters must be positive or zero")
        output_tokens = estimate_tokens_from_characters(output_characters)
        duration_seconds = time.monotonic() - self._started_at
        for record in self._records:
            record.output_characters = output_characters
            record.output_tokens = output_tokens
            record.duration_seconds = duration_seconds
        MonetaryCostManager._check_active_limits_after_usage_update()
        self._finished = True


class MonetaryCostManager:
    """
    Track crude OpenRouter LLM usage in the active async context.

    The tracker records input characters before each OpenRouter LLM call and
    output characters after the response. Tokens are estimated with the coarse
    rule requested for this project: 1 token = 4 characters.
    """

    _active_managers: ContextVar[list[MonetaryCostManager]] = ContextVar(
        "_active_monetary_cost_managers", default=[]
    )
    _id_counter: int = 0

    def __init__(
        self,
        hard_limit: float = 0,
        input_token_hard_limit: int = DEFAULT_INPUT_TOKEN_HARD_LIMIT,
        output_token_hard_limit: int = DEFAULT_OUTPUT_TOKEN_HARD_LIMIT,
        log_usage_when_called: bool = False,
        reserved_input_tokens: int = 0,
    ) -> None:
        if hard_limit < 0:
            raise ValueError("hard_limit must be positive or zero")
        if input_token_hard_limit < 0:
            raise ValueError("input_token_hard_limit must be positive or zero")
        if output_token_hard_limit < 0:
            raise ValueError("output_token_hard_limit must be positive or zero")
        if reserved_input_tokens < 0:
            raise ValueError("reserved_input_tokens must be positive or zero")
        if (
            reserved_input_tokens
            and input_token_hard_limit
            and reserved_input_tokens >= input_token_hard_limit
        ):
            logger.warning(
                "reserved_input_tokens (%d) >= input_token_hard_limit (%d): "
                "all reserve-gated research work will be skipped for this manager",
                reserved_input_tokens,
                input_token_hard_limit,
            )
        self.hard_limit: Final[float] = hard_limit
        self.input_token_hard_limit: Final[int] = input_token_hard_limit
        self.output_token_hard_limit: Final[int] = output_token_hard_limit
        self.reserved_input_tokens: Final[int] = reserved_input_tokens
        self._log_usage_when_called = log_usage_when_called
        self._records: list[OpenRouterUsageRecord] = []
        self._lock = threading.RLock()
        MonetaryCostManager._id_counter += 1
        self.id = MonetaryCostManager._id_counter

    @property
    def current_usage(self) -> float:
        """Estimated tokens that count against the hard limits (paid calls only)."""
        return float(self.budgeted_input_tokens + self.budgeted_output_tokens)

    # The hard limits and the compile/forecast reserve exist to bound PAID
    # spend. A free quota route (SoCLaaS Qwen) spends allowance, not money, so
    # its calls stay in the ledger and the totals but not in the budget. The
    # 45572 A/B run lost its focused artifact retry because free Qwen
    # extraction cycles had used up a reserve meant for paid calls.
    @property
    def budgeted_input_tokens(self) -> int:
        with self._lock:
            return sum(r.input_tokens for r in self._records if r.cost_source != "quota")

    @property
    def budgeted_output_tokens(self) -> int:
        with self._lock:
            return sum(r.output_tokens for r in self._records if r.cost_source != "quota")

    @property
    def amount_left(self) -> float:
        return self.hard_limit - self.current_usage

    @property
    def total_input_characters(self) -> int:
        with self._lock:
            return sum(record.input_characters for record in self._records)

    @property
    def total_input_tokens(self) -> int:
        with self._lock:
            return sum(record.input_tokens for record in self._records)

    @property
    def total_output_characters(self) -> int:
        with self._lock:
            return sum(record.output_characters for record in self._records)

    @property
    def total_output_tokens(self) -> int:
        with self._lock:
            return sum(record.output_tokens for record in self._records)

    @property
    def total_reasoning_tokens(self) -> int:
        with self._lock:
            return sum(record.reasoning_tokens for record in self._records)

    @property
    def total_native_input_tokens(self) -> int:
        with self._lock:
            return sum(record.native_input_tokens for record in self._records)

    @property
    def total_native_output_tokens(self) -> int:
        with self._lock:
            return sum(record.native_output_tokens for record in self._records)

    @property
    def total_quota_microdollars(self) -> float:
        """Metered spend on free-in-money routes. Not dollars; an allowance."""
        with self._lock:
            return sum(record.quota_microdollars for record in self._records)

    @property
    def total_paid_input_tokens(self) -> int:
        """Native input tokens on routes that actually bill money.

        The plain total mixes paid and free traffic, which stops being a
        useful number the moment a free endpoint carries most of the volume.
        """
        with self._lock:
            return sum(
                record.native_input_tokens
                for record in self._records
                if record.cost_source != "quota"
            )

    @property
    def total_paid_output_tokens(self) -> int:
        with self._lock:
            return sum(
                record.native_output_tokens
                for record in self._records
                if record.cost_source != "quota"
            )

    @property
    def endpoints_used(self) -> list[str]:
        with self._lock:
            seen = {r.endpoint for r in self._records if r.endpoint}
        return sorted(seen)

    @property
    def total_cached_input_tokens(self) -> int:
        with self._lock:
            return sum(record.cached_input_tokens for record in self._records)

    @property
    def total_cache_write_tokens(self) -> int:
        with self._lock:
            return sum(record.cache_write_tokens for record in self._records)

    @property
    def total_cost_usd(self) -> float:
        with self._lock:
            return sum(record.cost_usd for record in self._records)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def __enter__(self) -> MonetaryCostManager:
        managers = self._active_managers.get().copy()
        managers.append(self)
        self._active_managers.set(managers)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        managers = self._active_managers.get().copy()
        managers.remove(self)
        self._active_managers.set(managers)

    def get_usage_records(self) -> list[OpenRouterUsageRecord]:
        """Snapshot copies, so callers cannot mutate the live ledger.

        ``replace`` rather than a hand-written field list: the previous version
        enumerated every field, so any field added to OpenRouterUsageRecord was
        silently dropped from the copy while the totals -- which read
        ``self._records`` directly -- still saw it. That is invisible until a
        column renders empty next to a correct total.
        """
        with self._lock:
            return [replace(record) for record in self._records]

    def format_usage_yaml_table(self, key: str = "openrouter_llm_usage") -> str:
        records = self.get_usage_records()
        table = _format_usage_markdown_table(records)
        lines = [
            f"{key}:",
            f"  total_input_characters: {self.total_input_characters}",
            f"  total_input_tokens: {self.total_input_tokens}",
            f"  input_token_hard_limit: {self.input_token_hard_limit}",
            f"  total_output_characters: {self.total_output_characters}",
            f"  total_output_tokens: {self.total_output_tokens}",
            f"  total_reasoning_tokens: {self.total_reasoning_tokens}  # provider-reported; included in output billing, not in the char-based estimates above",
            f"  total_native_input_tokens: {self.total_native_input_tokens}  # {llm_provider.active_profile().name}-routed, provider-reported; ground truth vs the char-based estimates",
            f"  total_native_output_tokens: {self.total_native_output_tokens}",
            f"  total_cached_input_tokens: {self.total_cached_input_tokens}  # served from prompt cache (billed at the cache-read rate)",
            f"  total_cache_write_tokens: {self.total_cache_write_tokens}  # written to prompt cache (GPT-5.6+ bills these at 1.25x input)",
            f"  llm_provider: {llm_provider.LLM_PROVIDER}",
            f"  total_cost_usd: {self.total_cost_usd:.6f}  # {_cost_provenance(records)}",
            f"  total_paid_input_tokens: {self.total_paid_input_tokens}  # excludes free/quota routes",
            f"  total_paid_output_tokens: {self.total_paid_output_tokens}",
            f"  total_quota_microdollars: {self.total_quota_microdollars:.1f}  # metered allowance spent on free routes; NOT dollars",
            f"  endpoints_used: {self.endpoints_used}",
            f"  output_token_hard_limit: {self.output_token_hard_limit}",
            f"  total_tokens: {self.total_tokens}",
            f"  budgeted_tokens: {int(self.current_usage)}  # paid routes only; what the hard limits count",
            f"  total_token_hard_limit: {self.hard_limit}",
            f"  total_llm_call_seconds: {sum(r.duration_seconds for r in records):.1f}  # sum of per-call durations; parallel calls overlap in wall time",
            "  table: |",
        ]
        lines.extend(f"    {line}" for line in table.splitlines())
        return "\n".join(lines)

    @classmethod
    def get_active_cost_managers(cls) -> list[MonetaryCostManager]:
        return cls._active_managers.get()

    @classmethod
    def would_breach_input_reserve(cls, estimated_input_tokens: float = 0) -> bool:
        """True when optional research work should soft-stop.

        Checks every active manager that declares both an input hard limit and
        a reserved tail: would spending ``estimated_input_tokens`` more push
        total input past (limit - reserve)? Unlike
        ``raise_error_if_limit_would_be_reached`` this never raises — callers
        skip the optional step and proceed to compile/forecast with what they
        have, so budget exhaustion degrades the research, never the deliverable.
        Managers without a reserve never gate.
        """
        if estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be positive or zero")
        for manager in cls._active_managers.get():
            if not manager.input_token_hard_limit or not manager.reserved_input_tokens:
                continue
            research_budget = (
                manager.input_token_hard_limit - manager.reserved_input_tokens
            )
            if manager.budgeted_input_tokens + estimated_input_tokens > research_budget:
                return True
        return False

    @classmethod
    def raise_error_if_limit_would_be_reached(
        cls,
        amount_to_check_room_for: float = 0,
    ) -> None:
        if amount_to_check_room_for < 0:
            raise ValueError("amount_to_check_room_for must be positive or zero")
        for manager in cls._active_managers.get():
            next_total_input_tokens = (
                manager.budgeted_input_tokens + amount_to_check_room_for
            )
            if (
                manager.input_token_hard_limit
                and next_total_input_tokens > manager.input_token_hard_limit
            ):
                raise HardLimitExceededError(
                    f"Estimated input token usage {amount_to_check_room_for:.0f} would push "
                    f"total input tokens to {next_total_input_tokens:.0f}, exceeding the "
                    f"input token hard limit of {manager.input_token_hard_limit}"
                )

            if manager.hard_limit == 0:
                continue
            combined_limit_would_be_reached = (
                manager.amount_left <= 0
                if amount_to_check_room_for == 0
                else manager.amount_left < amount_to_check_room_for
            )
            if combined_limit_would_be_reached:
                raise HardLimitExceededError(
                    f"Estimated token usage {amount_to_check_room_for:.0f} would push "
                    f"current usage to {manager.current_usage + amount_to_check_room_for:.0f}, "
                    f"exceeding the hard limit of {manager.hard_limit:.0f}"
                )

    @classmethod
    def start_openrouter_call(
        cls,
        name_of_task: str,
        model: str,
        input_payload: Any,
    ) -> OpenRouterUsageHandle:
        input_characters = count_serialized_characters(input_payload)
        input_tokens = estimate_tokens_from_characters(input_characters)
        # Resolve the same route llm_client will use, so the ledger records
        # where the call actually went rather than where the default profile
        # would have sent it. route_for never raises; a name no profile claims
        # still yields a usable route.
        try:
            route = llm_provider.route_for(model, label=name_of_task)
        except Exception:  # noqa: BLE001 - accounting must never sink a call
            route = None
        if getattr(route, "cost_source", None) != "quota":
            cls.raise_error_if_limit_would_be_reached(input_tokens)
        # Every call site starts its ledger row immediately before create();
        # the Qwen ladder reads this to know which task it is serving.
        import qwen_ladder

        qwen_ladder.current_label.set(name_of_task)

        records: list[OpenRouterUsageRecord] = []
        for manager in cls._active_managers.get():
            with manager._lock:
                record = OpenRouterUsageRecord(
                    no=len(manager._records) + 1,
                    name_of_task=name_of_task,
                    input_characters=input_characters,
                    input_tokens=input_tokens,
                    output_characters=0,
                    output_tokens=0,
                    model_used=model,
                    endpoint=getattr(getattr(route, "endpoint", None), "label", ""),
                    cost_source=getattr(route, "cost_source", "") or "",
                )
                manager._records.append(record)
                records.append(record)
                if manager._log_usage_when_called:
                    logger.info(
                        "%s.ID%s recorded OpenRouter call #%s: %s | model=%s | input=%s chars/%s tokens",
                        manager.__class__.__name__,
                        manager.id,
                        record.no,
                        name_of_task,
                        model,
                        input_characters,
                        input_tokens,
                    )
        return OpenRouterUsageHandle(
            records, name_of_task=name_of_task, model=model, route=route
        )

    @classmethod
    def _check_active_limits_after_usage_update(cls) -> None:
        for manager in cls._active_managers.get():
            if (
                manager.output_token_hard_limit
                and manager.budgeted_output_tokens > manager.output_token_hard_limit
            ):
                raise HardLimitExceededError(
                    f"Estimated output token usage reached {manager.budgeted_output_tokens}, "
                    f"exceeding the output token hard limit of "
                    f"{manager.output_token_hard_limit}"
                )

            if manager.hard_limit == 0:
                continue
            if manager.current_usage > manager.hard_limit:
                raise HardLimitExceededError(
                    "Estimated token usage %.0f exceeded hard limit %.0f"
                    % (
                    manager.current_usage,
                    manager.hard_limit,
                    )
                )


def estimate_tokens_from_characters(character_count: int) -> int:
    if character_count < 0:
        raise ValueError("character_count must be positive or zero")
    if character_count == 0:
        return 0
    return math.ceil(character_count / CHARACTERS_PER_TOKEN)


def count_serialized_characters(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default))
    except (TypeError, ValueError):
        return len(str(value))


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def extract_openrouter_native_usage(
    response: Any, model: str = "", route: Any = None
) -> dict[str, Any]:
    """Provider-reported usage: native token counts and USD cost.

    On OpenRouter ``cost_usd`` sums OpenRouter credits (``usage.cost``,
    non-BYOK) and the upstream provider bill
    (``usage.cost_details.upstream_inference_cost``, BYOK) so it is correct
    under either billing mode; it requires the request to carry
    OPENROUTER_USAGE_ACCOUNTING (llm_provider.chat_kwargs sends it).

    The OpenAI API reports no per-request cost at any endpoint -- the Costs API
    is daily-bucketed and needs an admin key, and the Usage API returns tokens
    without dollars -- so there ``cost_usd`` is computed from the local price
    table against the token counts OpenAI does return. ``model`` is used for
    that lookup and falls back to the model echoed in the response.

    ``route`` selects HOW cost is derived, and should always be supplied now
    that one run can span several endpoints. Falling back to the global
    ``is_openai()`` when it is absent is a compatibility shim, and a wrong one
    under a mixed profile: it would price an OpenRouter-served call with the
    local table, or read a cost field off a response that never carried one,
    purely because of a process-wide flag. That is the same order-of-operations
    error the Route table exists to remove -- the policy has to come from the
    route that served the call, not from the process.

    Missing fields read as 0, so a provider that omits the breakdown degrades
    gracefully.
    """
    usage = _get_field(response, "usage")
    prompt_details = _get_field(usage, "prompt_tokens_details")
    cost_details = _get_field(usage, "cost_details")

    prompt_tokens = _coerce_int(_get_field(usage, "prompt_tokens"))
    completion_tokens = _coerce_int(_get_field(usage, "completion_tokens"))
    cached_tokens = _coerce_int(_get_field(prompt_details, "cached_tokens"))
    cache_write_tokens = _coerce_int(_get_field(prompt_details, "cache_write_tokens"))

    cost_source = getattr(route, "cost_source", None) or (
        "price_table" if llm_provider.is_openai() else "native"
    )

    cost_usd = 0.0
    quota_microdollars = 0.0
    if cost_source == "price_table":
        cost_usd = llm_provider.compute_cost_usd(
            model or str(_get_field(response, "model") or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        )
    elif cost_source == "quota":
        # No money changes hands, so cost_usd stays 0.0 -- but the call still
        # spends a metered allowance, and the rates are per 1M tokens exactly
        # like PRICES.
        rates = getattr(route, "quota_rate_microdollars", None)
        if rates:
            in_rate, out_rate = rates
            quota_microdollars = (
                prompt_tokens * in_rate + completion_tokens * out_rate
            ) / 1_000_000
    else:  # native
        cost_usd = _coerce_float(_get_field(usage, "cost")) + _coerce_float(
            _get_field(cost_details, "upstream_inference_cost")
        )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": cost_usd,
        "cost_source": cost_source,
        "quota_microdollars": quota_microdollars,
    }


def count_openrouter_reasoning_tokens(response: Any) -> int:
    """Provider-reported reasoning tokens (0 when the model didn't think or the
    provider omitted the breakdown)."""
    usage = _get_field(response, "usage")
    details = _get_field(usage, "completion_tokens_details")
    value = _get_field(details, "reasoning_tokens")
    try:
        return max(0, int(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def count_openrouter_output_characters(response: Any) -> int:
    output_parts: list[str] = []
    choices = _get_field(response, "choices")
    if isinstance(choices, list):
        for choice in choices:
            message = _get_field(choice, "message")
            content = _get_field(message, "content")
            if content:
                output_parts.append(str(content))

            tool_calls = _get_field(message, "tool_calls")
            if tool_calls:
                output_parts.append(_stable_string(tool_calls))

            text = _get_field(choice, "text")
            if text:
                output_parts.append(str(text))

    if output_parts:
        return len("\n".join(output_parts))
    return count_serialized_characters(response)


_COST_SOURCE_BLURB: Final = {
    "native": "provider-reported actual cost (credits + BYOK upstream)",
    "price_table": "computed from llm_provider.PRICES (no per-request cost served)",
    "quota": "free in money; metered against a request/token allowance instead",
}


def _cost_provenance(records: list[OpenRouterUsageRecord] | None = None) -> str:
    """How the dollar figures in this ledger were arrived at.

    Derived from the rows rather than from a process-wide provider flag: once
    one run can span several endpoints, a single global answer is wrong for
    some of the rows it claims to describe. Falls back to the old global
    reading only for an empty ledger.
    """
    sources = sorted({r.cost_source for r in (records or []) if r.cost_source})
    if not sources:
        if llm_provider.is_openai():
            return _COST_SOURCE_BLURB["price_table"]
        return _COST_SOURCE_BLURB["native"]
    if len(sources) == 1:
        return _COST_SOURCE_BLURB.get(sources[0], sources[0])
    return "mixed: " + "; ".join(
        f"{s} = {_COST_SOURCE_BLURB.get(s, s)}" for s in sources
    )


def _format_usage_markdown_table(records: list[OpenRouterUsageRecord]) -> str:
    lines = [
        "| no. | name of task | input characters | input tokens | output characters | output tokens | reasoning tokens | native in | native out | cached in | cache write | cost usd | quota ud | seconds | endpoint | model used |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for record in records:
        lines.append(
            "| "
            f"{record.no} | "
            f"{_table_cell(record.name_of_task)} | "
            f"{record.input_characters} | "
            f"{record.input_tokens} | "
            f"{record.output_characters} | "
            f"{record.output_tokens} | "
            f"{record.reasoning_tokens} | "
            f"{record.native_input_tokens} | "
            f"{record.native_output_tokens} | "
            f"{record.cached_input_tokens} | "
            f"{record.cache_write_tokens} | "
            f"{record.cost_usd:.6f} | "
            f"{record.quota_microdollars:.1f} | "
            f"{record.duration_seconds:.1f} | "
            f"{_table_cell(record.endpoint or '?')} | "
            f"{_table_cell(record.model_used)} |"
        )
    return "\n".join(lines)


def _table_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _stable_string(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
    except (TypeError, ValueError):
        return str(value)


async def get_openrouter_key_usage(api_key: str) -> dict[str, Any]:
    """
    Return usage/limit data for the active OpenRouter API key.

    The key endpoint exposes limit_remaining, which is useful for comparing
    OpenRouter's billing-side accounting against this local character ledger.
    """
    if not api_key:
        raise ValueError("OpenRouter API key is required to fetch key usage")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            OPENROUTER_KEY_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("OpenRouter key usage response did not include data")
    return data
