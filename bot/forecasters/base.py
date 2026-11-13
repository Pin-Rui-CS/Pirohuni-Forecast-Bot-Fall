from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import datetime
from contextvars import ContextVar

from bot.config import (
    FORECAST_MAX_OUTPUT_TOKENS,
    FORECASTER_BACKUP_MODEL,
    FORECASTER_MODELS,
    FORECASTER_WEIGHTS,
    HETEROGENEOUS_RUN_ENABLED,
    HETEROGENEOUS_RUN_MODEL,
    SHADOW_FORECASTER_MODELS,
    SHADOW_FORECASTER_UNTIL,
)
from bot.trace import research_trace
from bot.llm.client import call_llm

logger = logging.getLogger(__name__)

# A shadow run may take as long as a forecast run, but must never hold up a
# question that has its real forecasts.
SHADOW_TIMEOUT_SECONDS = 900
# The current question's shadow records, set by gather_forecast_runs in the
# question's own task and collected by the orchestrator (take_shadow_records).
_SHADOW_RECORDS: ContextVar[list[dict] | None] = ContextVar("shadow_records", default=None)


def shadow_models(today: datetime.date | None = None) -> list[str]:
    """The models on trial right now: SHADOW_FORECASTER_MODELS, or none once
    SHADOW_FORECASTER_UNTIL has passed (an unreadable date also means none --
    a trial that cannot tell when it ends does not run)."""
    if not SHADOW_FORECASTER_MODELS:
        return []
    if SHADOW_FORECASTER_UNTIL:
        try:
            until = datetime.date.fromisoformat(SHADOW_FORECASTER_UNTIL)
        except ValueError:
            logger.warning("[shadow] SHADOW_FORECASTER_UNTIL=%r is not YYYY-MM-DD; shadow runs are off.",
                           SHADOW_FORECASTER_UNTIL)
            return []
        if (today or datetime.date.today()) > until:
            return []
    return list(SHADOW_FORECASTER_MODELS)


def take_shadow_records() -> list[dict]:
    """The shadow records of the question just forecast (and clear them)."""
    records = _SHADOW_RECORDS.get() or []
    _SHADOW_RECORDS.set(None)
    return records


# Prepended to the research handed to the heterogeneous (raw-input) ensemble
# member so it knows why its material looks different from a compiled brief.
RAW_RESEARCH_NOTE = (
    "NOTE: This run receives the RAW research output (deduplicated but NOT "
    "compiled into a brief). It exists to catch anything a compilation step "
    "might have dropped or skewed, so read the material yourself and weigh "
    "ALL of it — including search-result snippets, which sometimes hold "
    "decisive evidence that appears nowhere else. There are no [E#] labels "
    "here; cite evidence by source name or URL."
)


@dataclass
class ForecastResult:
    """Everything the orchestrator needs from a forecaster.

    ``forecast`` is payload-ready (probability, per-option dict, or CDF list).
    ``run_values`` holds the per-run parsed forecasts for forecast.json.
    """

    forecast: Any
    comment: str
    prompt: str
    run_transcripts: list[str]
    run_values: list[Any]
    extra: dict = field(default_factory=dict)


@dataclass
class ForecastRun:
    """One ensemble member's single-shot forecast attempt.

    ``valid`` reflects whether ``response`` passed the caller's validator (after
    an optional one-shot repair). Invalid runs are still returned — their
    transcript is worth saving — but callers drop them from aggregation and
    attribute the failure to ``model``.
    """

    model: str
    response: str
    transcript: str
    repaired: bool = False
    valid: bool = True
    error: str | None = None
    backup_for: str | None = None  # the model this run stood in for, when it is a backup


# A validator returns None when a response is usable, or a short error string
# describing why it is not. The string drives a single repair retry and is
# recorded for per-model accounting.
Validator = Callable[[str], "str | None"]


def short_model_name(model: str) -> str:
    """`openai/gpt-5.5` -> `gpt-5.5`, for compact labels, logs, and comments."""
    return model.split("/")[-1]


def parse_weights(spec: str) -> dict[str, float]:
    """``"model=share,..."`` -> {model: share}. A malformed entry, a share
    outside (0, 1], or shares adding past 1 are logged and ignored, falling
    back toward equal weights rather than failing a forecast."""
    shares: dict[str, float] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        model, _, value = part.partition("=")
        try:
            share = float(value)
        except ValueError:
            logger.warning("[weights] ignoring %r: not model=share", part.strip())
            continue
        if not model.strip() or not 0 < share <= 1:
            logger.warning("[weights] ignoring %r: share must be in (0, 1]", part.strip())
            continue
        shares[model.strip()] = share
    if sum(shares.values()) > 1 + 1e-9:
        logger.warning("[weights] shares %s add up to more than 1; using equal weights", shares)
        return {}
    return shares


def run_weights(models: list[str], spec: str | None = None) -> list[float]:
    """One weight per run for the ensemble average, summing to 1.

    A model named in FORECASTER_WEIGHTS gets its share, split across its runs if
    it ran more than once; every other run splits what is left equally. Weights
    follow the model that ACTUALLY ran: a backup standing in for Opus gets an
    ordinary share, not Opus's. With no named model present, weights are equal;
    with only named models present, their shares are scaled to sum to 1.
    """
    if not models:
        return []
    shares = parse_weights(FORECASTER_WEIGHTS if spec is None else spec)
    counts = {m: models.count(m) for m in set(models)}
    named = {m: shares[m] for m in counts if m in shares}
    others = [m for m in models if m not in named]
    rest = max(0.0, 1.0 - sum(named.values()))
    if not named:
        return [1.0 / len(models)] * len(models)
    if not others:
        total = sum(named.values())
        return [named[m] / total / counts[m] for m in models]
    return [named[m] / counts[m] if m in named else rest / len(others) for m in models]


def models_for_runs(num_runs: int, models: list[str] | None) -> list[str]:
    """Assign a model to each run, cycling the pool in order when there are more
    runs than models. Falls back to the configured pool when ``models`` is None."""
    pool = list(models) if models else list(FORECASTER_MODELS)
    if not pool:
        raise ValueError("No forecaster models configured (FORECASTER_MODELS is empty).")
    return [pool[i % len(pool)] for i in range(max(0, num_runs))]


def heterogeneous_run_setup(
    num_runs: int,
    brief_prompt: str,
    raw_prompt: str | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Swap the LAST ensemble run to the raw-research prompt on the
    heterogeneous model.

    Returns ``(run_prompts, models)`` for gather_forecast_runs, or
    ``(None, None)`` when the swap does not apply (disabled, no raw view, or a
    single-run ensemble — the compiled-brief run is never given up entirely).
    It is a swap, not an addition: N same-model runs on one brief reproduce any
    brief-level skew at full weight, so the third identical run buys almost no
    information, while a raw-input run is the only member able to catch
    evidence the compile step dropped.
    """
    if not HETEROGENEOUS_RUN_ENABLED or not raw_prompt or num_runs < 2:
        return None, None
    run_prompts = [brief_prompt] * num_runs
    run_prompts[-1] = raw_prompt
    models = models_for_runs(num_runs, None)
    models[-1] = HETEROGENEOUS_RUN_MODEL
    return run_prompts, models


async def gather_forecast_runs(
    prompt: str,
    num_runs: int,
    label: str,
    *,
    temperature: float = 0.3,
    models: list[str] | None = None,
    validate: Validator | None = None,
    repair_instruction: str | None = None,
    run_prompts: list[str] | None = None,
    backup: str | None = None,
    shadow_parse: Callable[[str], Any] | None = None,
) -> list[ForecastRun]:
    """Run the forecast prompt ``num_runs`` times in parallel across an ensemble
    of models — one model per run, cycling the pool.

    Different provider lineages decorrelate their errors; aggregating across them
    is where the accuracy comes from. The prompt is marked as a cacheable prefix
    so providers with prompt caching can reuse it across runs and any follow-up
    (e.g. tiebreaker) call.

    When ``validate`` is supplied, a run whose response fails validation gets
    exactly one repair retry (same model, prompt + ``repair_instruction``). This
    is the con-1 (format-compliance) defence: weaker format-followers get a
    second, pointed chance instead of silently degrading the ensemble, and every
    failure/repair is logged against its model (con-2 attribution).

    Note: we deliberately do NOT use OpenRouter JSON-mode / ``response_format``
    here. These prompts reason in prose through several phases and emit their
    answer at the very end; forcing whole-message JSON would destroy that
    reasoning (and the rationales the binary tiebreaker and human reviewers
    read). Robust parsing + a targeted repair retry fits the format instead of
    fighting it.

    ``run_prompts`` optionally overrides the prompt per run (index-aligned with
    the runs) — used by the heterogeneous ensemble member, which reads the raw
    research instead of the compiled brief. When omitted, every run uses
    ``prompt``.

    ``backup`` (default FORECASTER_BACKUP_MODEL): a brief run that still fails
    after the steps above is redone once on this model, which then takes the
    run's place. Not for the raw-research run, and not when the failed run
    was already on the backup model.
    """
    assigned = models_for_runs(num_runs, models)
    backup = FORECASTER_BACKUP_MODEL if backup is None else backup
    prompts = list(run_prompts) if run_prompts is not None else [prompt] * len(assigned)
    if len(prompts) != len(assigned):
        raise ValueError(
            f"run_prompts length {len(prompts)} != num_runs {len(assigned)}"
        )

    async def _call(call_prompt: str, model: str, sublabel: str) -> tuple[str, str]:
        # No cache_static_prefix here. Anthropic bills a cache WRITE at 1.25x
        # input, and this cache can never be read: no two calls in a question
        # share (model, prefix) -- runs 1 and 2 share a prompt but differ in
        # model, and the heterogeneous run differs in both. Measured on 44879:
        # the flag added $0.042 to a single Sonnet run (83,842 tok x $0.50/M)
        # against `total_cached_input_tokens: 0` for the whole run.
        # Re-enable only together with a real shared prefix: the ~11.9K chars of
        # scaffold that ARE identical across questions currently sit AFTER the
        # variable research in the template, where no prefix cache can reach
        # them. Moving them first would be worth only ~$0.005/call.
        return await call_llm(
            call_prompt,
            model=model,
            temperature=temperature,
            _label=sublabel,
            return_transcript=True,
            max_tokens=FORECAST_MAX_OUTPUT_TOKENS,
            deliberate=True,
            # An answer cut off at the cap never reached its final numbers,
            # but the parsers read numbers from the unfinished reasoning as if
            # they were the answer: Q46579 (2026-11-08) Sonnet stopped at 20k
            # and was read as Cyan 42% / Yellow 42%. Raising makes it a failed
            # call: the run is redone on the backup model, or dropped.
            raise_on_truncation=True,
        )

    def _short_error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {str(exc)[:200]}"

    async def one_run(index: int, model: str) -> ForecastRun:
        run = await _one_run(index, model)
        raw = prompts[index] != prompt
        if not run.valid and backup and not raw and model != backup:
            # The failed attempt keeps its own lineage node (it writes nothing);
            # the backup's node is the one that produces this run's forecast.
            _emit(index, model, run, raw, final=False)
            logger.warning(
                "[ensemble] %s run %d/%d (%s) failed (%s) — redoing it on backup %s.",
                label, index + 1, len(assigned), model, run.error, backup,
            )
            run = await _one_run(index, backup)
            run.backup_for = model
        _emit(index, run.model, run, raw, final=True)
        return run

    def _emit(index: int, model: str, run: ForecastRun, raw: bool, *, final: bool) -> None:
        # Lineage: one node per run, reading the brief or (the heterogeneous
        # member) the raw research view.
        name = short_model_name(model)
        if run.backup_for:
            name += f" (backup for {short_model_name(run.backup_for)})"
        research_trace.emit(
            "forecast",
            f"{label} run {index + 1} · {name}" + (" (raw research)" if raw else ""),
            run.transcript or "",
            status="ok" if run.valid else "failed",
            error=run.error or "",
            meta={"model": model, "repaired": run.repaired, "backup_for": run.backup_for},
            reads=[research_trace.ref_doc("raw_view" if raw else "brief")],
            writes=[research_trace.ref_doc(f"forecast:{label}:{index + 1}")] if final else [],
            kind="model", lane="*",
        )

    async def _one_run(index: int, model: str) -> ForecastRun:
        tag = short_model_name(model)
        run_prompt = prompts[index]
        # A hard call failure (provider down, rate-limited, quota exhausted) must
        # degrade the ensemble to the surviving models, never sink the whole
        # question — that is the con-2 failure mode in its starkest form.
        try:
            response, transcript = await _call(run_prompt, model, f"{label}[{tag}]")
        except Exception as exc:  # noqa: BLE001 - any provider failure is recoverable here
            err = _short_error(exc)
            logger.warning(
                "[ensemble] %s run %d/%d (%s) call failed: %s — dropping this run.",
                label, index + 1, len(assigned), model, err,
            )
            return ForecastRun(
                model=model, response="", transcript=f"# call failed for {model}\n{err}",
                valid=False, error=f"call failed: {err}",
            )

        if validate is None:
            return ForecastRun(model=model, response=response, transcript=transcript)

        error = validate(response)
        if error is None:
            return ForecastRun(model=model, response=response, transcript=transcript)

        logger.warning(
            "[ensemble] %s run %d/%d (%s) failed validation: %s — repairing once.",
            label, index + 1, len(assigned), model, error,
        )
        repair_prompt = run_prompt
        if repair_instruction:
            repair_prompt = (
                f"{run_prompt}\n\n---\n\n"
                f"Your previous response could not be used: {error}\n"
                f"{repair_instruction}"
            )
        try:
            response, transcript = await _call(repair_prompt, model, f"{label}[{tag}]/repair")
        except Exception as exc:  # noqa: BLE001 - repair-call failure: drop, don't crash
            err = _short_error(exc)
            logger.warning(
                "[ensemble] %s run %d/%d (%s) repair call failed: %s — dropping this run.",
                label, index + 1, len(assigned), model, err,
            )
            return ForecastRun(
                model=model, response=response, transcript=transcript,
                repaired=True, valid=False, error=f"repair call failed: {err}",
            )
        error = validate(response)
        if error is not None:
            logger.warning(
                "[ensemble] %s run %d/%d (%s) still invalid after repair: %s",
                label, index + 1, len(assigned), model, error,
            )
        return ForecastRun(
            model=model,
            response=response,
            transcript=transcript,
            repaired=True,
            valid=error is None,
            error=error,
        )

    async def shadow_run(model: str) -> dict:
        """One trial forecast from the brief prompt. Recorded, never counted:
        it is not in the returned runs, so no aggregate, tiebreaker or comment
        can see it. Any failure -- call error, cut-off answer, time-out,
        unparseable reply, budget refusal -- only marks the record invalid."""
        tag = short_model_name(model)
        record: dict = {"model": model, "valid": False}
        transcript = ""
        try:
            response, transcript = await asyncio.wait_for(
                _call(prompt, model, f"{label}[{tag}]/shadow"), SHADOW_TIMEOUT_SECONDS)
            error = validate(response) if validate else None
            if error is None and shadow_parse is not None:
                record["value"] = shadow_parse(response)
            record["valid"] = error is None
            if error is not None:
                record["error"] = str(error)[:500]
        except BaseException as exc:  # noqa: BLE001 - a trial run must never sink the question
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            record["valid"] = False
            record.pop("value", None)
            record["error"] = _short_error(exc)
        logger.info("[shadow] %s %s: %s", label, model,
                    "ok" if record["valid"] else f"invalid ({record.get('error')})")
        research_trace.emit(
            "shadow_forecast", f"{label} shadow · {tag} (not counted)",
            transcript or f"# shadow run failed for {model}\n{record.get('error', '')}",
            status="ok" if record["valid"] else "failed", error=record.get("error", ""),
            meta={"model": model, "counted": False},
            reads=[research_trace.ref_doc("brief")], writes=[], kind="model", lane="*",
        )
        return record

    shadows = shadow_models()
    results = await asyncio.gather(
        *[one_run(i, m) for i, m in enumerate(assigned)],
        *[shadow_run(m) for m in shadows],
    )
    if shadows:
        _SHADOW_RECORDS.set(list(results[len(assigned):]))
    return list(results[:len(assigned)])
