"""Single point of divergence between every LLM endpoint the bot can reach.

Where a call goes, what the model is called there, how the request must be
shaped, and how it is priced -- all of it lives here. The rest of the bot keeps
calling ``chat.completions.create`` with the same logical payload it always
did; this module rewrites that payload for whichever endpoint is active.

Select a routing mode with one env var::

    LLM_ROUTING=openrouter    # default -- everything through OpenRouter
    LLM_ROUTING=openai        # everything through the direct OpenAI API

``LLM_PROVIDER`` is still honoured as the old name for the same setting, so
existing .env files and CI variables keep working unchanged.

Why a translation layer instead of editing every call site: the bot names its
models in 18 places across 13 files (``anthropic/claude-opus-5.5`` for the
forecaster/compiler/tiebreaker roles, ``anthropic/claude-sonnet-5`` for the
research utilities). Those names carry the *role*, not a vendor commitment.
``route_for`` maps them onto a concrete endpoint and id at the client boundary,
so switching endpoints touches no call site and reverting is one env var.

THE ROUTE TABLE, and why the obvious design does not work
---------------------------------------------------------
A single global "which provider are we on?" flag is enough only while every
model sits behind one base URL. It stops being enough the moment two endpoints
must serve one run -- a paid Tier-1 model alongside a free Tier-2 one, say.

The obstacle is not the URL, it is the ORDER OF OPERATIONS. The old code
resolved a model name to a concrete id and only *then* chose the request
policy, so the internal name -- the only thing identifying the role -- was gone
before the policy was picked. With one endpoint that is harmless, because the
policy is global. With two it is wrong: the same concrete id can legitimately
sit in two routes wanting different treatment.

So the unit that travels is a :class:`Route`, not a string. A Route pairs an
:class:`Endpoint` with a model id and a set of capability flags, and those
flags are keyed on the MODEL FAMILY, not on the provider. That distinction is
the whole point: ``temperature`` is rejected by the GPT-5/6 reasoning models
wherever they are served and accepted by open-weight models wherever they are
served, so "is this OpenAI?" gets it wrong in both directions as soon as a
reasoning model is reached through a broker.

The differences the flags encode, each of which would otherwise fail hard:

1. ``accepts_temperature`` -- the GPT-5/6 reasoning models reject it outright
   (400 unsupported_value; only the default 1 is allowed). OpenRouter
   normalises it away instead, which is why the gpt-5.6-sol ensemble member
   works today.
2. ``sends_usage_accounting`` -- ``extra_body={"usage": {"include": True}}`` is
   an OpenRouter extension that makes it report real per-call cost. OpenAI
   400s on unrecognised body arguments, so there we price locally from PRICES.
3. ``uses_max_completion_tokens`` / ``reasoning_headroom_tokens`` -- on the
   direct OpenAI API ``max_tokens`` becomes ``max_completion_tokens``, and that
   cap must also cover the model's internal reasoning. A cap sized for the
   visible answer alone gets consumed entirely by reasoning, returning an empty
   message with finish_reason=length.
4. ``supports_cache_control`` -- Anthropic-style ``cache_control`` breakpoints
   in message content parts mean something on OpenRouter and nothing on OpenAI.
5. ``prompt_cache_mode`` -- GPT-5.6+ bills a cache WRITE at 1.25x the uncached
   input rate and caches automatically. Every prompt here is unique to one
   question, so the default buys a ~8% input surcharge on writes that can never
   be read; mode=explicit with no breakpoints turns that off.
6. ``cost_source`` -- ``native`` (the response carries a cost field),
   ``price_table`` (compute locally), or ``quota`` (free in money, but consuming
   a real request/token budget that still has to be tracked).

Behavioural equivalence with the pre-route-table implementation is pinned by
``tests/golden_chat_kwargs.json`` -- 550 request snapshots covering every
internal model name, every payload shape in the repo, both routing modes, and
the env overrides that change reasoning effort and prompt caching. Run
``python tests/test_route_equivalence.py`` after touching request shaping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Final

import dotenv

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

PROVIDER_OPENROUTER: Final = "openrouter"
PROVIDER_OPENAI: Final = "openai"
_VALID_PROVIDERS: Final = frozenset({PROVIDER_OPENROUTER, PROVIDER_OPENAI})


def _read_provider() -> str:
    """The routing mode for this process.

    ``LLM_ROUTING`` is the forward-looking name (a mode can now span several
    endpoints, so "provider" is the wrong word). ``LLM_PROVIDER`` stays
    honoured verbatim so every existing .env and GitHub Actions variable keeps
    working -- which is also the revert path.
    """
    raw = (
        os.getenv("LLM_ROUTING") or os.getenv("LLM_PROVIDER") or PROVIDER_OPENROUTER
    ).strip().lower()
    if raw not in _VALID_PROVIDERS:
        logger.warning(
            "Routing mode %r is not one of %s -- falling back to %r.",
            raw,
            sorted(_VALID_PROVIDERS),
            PROVIDER_OPENROUTER,
        )
        return PROVIDER_OPENROUTER
    return raw


LLM_PROVIDER: Final[str] = _read_provider()

OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL: Final = "https://api.openai.com/v1"


def is_openai() -> bool:
    return LLM_PROVIDER == PROVIDER_OPENAI


def provider_name() -> str:
    """Human-readable provider label for logs and error messages."""
    return "OpenAI" if is_openai() else "OpenRouter"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def api_key() -> str:
    """The API key for the active provider ("" when unset)."""
    name = "OPENAI_API_KEY" if is_openai() else "OPENROUTER_API_KEY"
    return (os.getenv(name) or "").strip()


def api_key_env_var() -> str:
    return "OPENAI_API_KEY" if is_openai() else "OPENROUTER_API_KEY"


def base_url() -> str:
    return OPENAI_BASE_URL if is_openai() else OPENROUTER_BASE_URL


# ---------------------------------------------------------------------------
# Model translation
# ---------------------------------------------------------------------------
# Left side: the role-carrying name the bot uses internally. Right side: the
# OpenAI model that plays that role. Chosen on measured cost parity -- the
# ledger for question 44875 solves exactly to $2/$10 for claude-sonnet-5 and
# $5/$25 for claude-opus-5, so sol ($5/$30) and terra ($2/$12) are near-swaps
# rather than a step change in spend. The Tier-1 role moved to
# claude-opus-5.5 ($4/$20) on 2026-09-23, so sol is now the pricier side of
# that swap on output; it stays the closest OpenAI equivalent.
MODEL_TRANSLATION: Final[dict[str, str]] = {
    "anthropic/claude-opus-5.5": "gpt-5.6-sol",
    "anthropic/claude-sonnet-5": "gpt-5.6-terra",
    # Already-OpenAI names: strip the OpenRouter vendor prefix.
    "openai/gpt-5.6-sol": "gpt-5.6-sol",
    "openai/gpt-5.6-terra": "gpt-5.6-terra",
    "openai/gpt-5.6-luna": "gpt-5.6-luna",
}


def resolve_model(model: str) -> str:
    """Map an internal model name onto the active provider's namespace.

    Under OpenRouter this is the identity. Under OpenAI it applies
    MODEL_TRANSLATION, and falls back to stripping a ``vendor/`` prefix so an
    unmapped name still produces a plausible OpenAI id rather than a 404 on a
    slash-containing string.
    """
    if not is_openai():
        return model
    mapped = MODEL_TRANSLATION.get(model)
    if mapped is not None:
        return mapped
    if "/" in model:
        stripped = model.split("/")[-1]
        logger.warning(
            "No OpenAI translation for model %r; falling back to %r. "
            "Add it to MODEL_TRANSLATION in llm_provider.py.",
            model,
            stripped,
        )
        return stripped
    return model


# ---------------------------------------------------------------------------
# Reasoning effort
# ---------------------------------------------------------------------------
# Effort is a weak cost lever for this bot -- input is roughly 70% of the bill,
# so moving the four deliverable-producing calls from medium to high costs
# about +14% of a question. It is a strong *quality* lever on exactly those
# calls, and near-irrelevant on the research utilities (measured on 44875 they
# emit 0-310 reasoning tokens each). Hence: high where the forecast is decided,
# low everywhere else.
DEFAULT_REASONING_EFFORT: Final = "low"
REASONING_EFFORT_BY_MODEL: Final[dict[str, str]] = {
    "gpt-5.6-sol": "high",
    "gpt-5.6-terra": "low",
    "gpt-5.6-luna": "low",
}

# Extra completion-token budget to reserve for internal reasoning, on top of
# the caller's visible-output cap. OpenAI's guidance is to reserve at least
# 25,000 tokens for reasoning plus output; the bot's existing caps (compiler
# 10,000, resolution scraper 1,200) were sized for visible output only and
# would otherwise be eaten by reasoning, returning an empty message with
# finish_reason=length.
REASONING_HEADROOM_TOKENS: Final[dict[str, int]] = {
    "none": 0,
    "minimal": 1_000,
    "low": 4_000,
    "medium": 12_000,
    "high": 24_000,
    "xhigh": 48_000,
    "max": 64_000,
}
MAX_COMPLETION_TOKENS_CEILING: Final = 128_000


def reasoning_effort_for(resolved_model: str) -> str:
    """Reasoning effort for an already-resolved OpenAI model name.

    ``OPENAI_REASONING_EFFORT`` overrides every model when set; per-model
    overrides use ``OPENAI_REASONING_EFFORT_<MODEL>`` with dots and dashes
    replaced by underscores (e.g. OPENAI_REASONING_EFFORT_GPT_5_6_SOL=xhigh).
    """
    override = (os.getenv("OPENAI_REASONING_EFFORT") or "").strip().lower()
    if override:
        return override
    slug = resolved_model.replace("-", "_").replace(".", "_").upper()
    per_model = (os.getenv(f"OPENAI_REASONING_EFFORT_{slug}") or "").strip().lower()
    if per_model:
        return per_model
    return REASONING_EFFORT_BY_MODEL.get(resolved_model, DEFAULT_REASONING_EFFORT)


def max_completion_tokens_for(visible_max_tokens: int, effort: str) -> int:
    """Grow a visible-output cap to also cover internal reasoning.

    NOTE: on a reasoning model there is no parameter that caps visible output
    independently of reasoning, so this necessarily also permits a longer
    answer. Callers that care about answer length (the compiler's brief) keep
    enforcing it through the prompt and their own finish_reason check.
    """
    headroom = REASONING_HEADROOM_TOKENS.get(effort, REASONING_HEADROOM_TOKENS["low"])
    return min(MAX_COMPLETION_TOKENS_CEILING, max(1, visible_max_tokens) + headroom)


# ---------------------------------------------------------------------------
# Prompt caching
# ---------------------------------------------------------------------------
# GPT-5.6 charges 1.25x the uncached input rate for cache WRITES, and automatic
# caching is on by default. Every prompt this bot sends is unique to one
# question, so the default would buy a ~8% input surcharge on writes that can
# never be read -- the same trap forecasters/base.py:155 already documents for
# Anthropic, measured there at $0.042 on a single run against
# total_cached_input_tokens: 0. Per OpenAI's docs, setting mode=explicit with
# no breakpoints means the request "does not use prompt caching or incur
# cache-write charges".
#
# Set OPENAI_PROMPT_CACHE=implicit to let automatic caching back on (worth it
# only if you make several calls that share a long identical prefix).
PROMPT_CACHE_CAPABLE_PREFIXES: Final = ("gpt-5.6",)


def prompt_cache_options_for(resolved_model: str) -> dict[str, Any] | None:
    """``prompt_cache_options`` body field, or None when it must not be sent.

    Only GPT-5.6 and later accept this field, so it is gated on the model name
    to avoid a 400 on older ones.
    """
    mode = (os.getenv("OPENAI_PROMPT_CACHE") or "explicit").strip().lower()
    if mode == "implicit":
        return None
    if not resolved_model.startswith(PROMPT_CACHE_CAPABLE_PREFIXES):
        return None
    return {"mode": "explicit"}


def supports_cache_control_breakpoints() -> bool:
    """Whether Anthropic-style ``cache_control`` content parts are meaningful.

    True only on OpenRouter, which forwards them to providers that understand
    them. OpenAI has no equivalent in the message body.
    """
    return not is_openai()


# ---------------------------------------------------------------------------
# Pricing ($/1M tokens: input, cached input, output)
# ---------------------------------------------------------------------------
# OpenAI reports no per-request cost at any endpoint -- the Costs API is
# daily-bucketed and needs a separate admin key, and the Usage API returns
# tokens without dollars. So the `cost usd` column that every audit table and
# post-mortem in this repo depends on has to be computed locally.
#
# Keep this table in sync with https://developers.openai.com/api/docs/pricing
PRICES: Final[dict[str, tuple[float, float, float]]] = {
    # GPT-6 Astra. BOTH keys are required, not just the base one:
    # _normalise_price_key pops trailing "-"-segments until a key matches, so
    # with only "gpt-6-astra" present, "gpt-6-astra-pro" would silently price
    # as non-pro. Pro is the same underlying model served with
    # reasoning.mode=pro and carries the SAME sticker price -- its extra cost
    # is entirely extra reasoning tokens billed as output, which is exactly
    # what a wrong-key lookup would hide.
    #
    # Output is 5x the input rate here, inverting the assumption the rest of
    # this repo was tuned under (Opus-5 at $5/$25, now Opus-5.5 at $4/$20,
    # where input dominated).
    # Output caps, not input budgets, are the cost control for these models.
    #
    # Routing to the OpenAI Flex endpoint halves both rates to $5/$25. That is
    # a provider choice, not a different model id, so it is not a key here --
    # via OpenRouter the real per-call cost comes back natively and this table
    # is not consulted at all.
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-astra-pro": (10.00, 1.00, 50.00),
    "gpt-5.6-sol": (5.00, 0.50, 30.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}
# GPT-5.6+ bills a cache write at 1.25x the uncached input rate.
CACHE_WRITE_MULTIPLIER: Final = 1.25


def _normalise_price_key(model: str) -> str:
    """Strip vendor prefix and any trailing date/version snapshot suffix."""
    name = model.split("/")[-1].strip().lower()
    if name in PRICES:
        return name
    # "gpt-5.6-sol-2026-07-01" -> "gpt-5.6-sol": drop trailing segments until a
    # known key matches, so dated snapshot ids price correctly.
    parts = name.split("-")
    while len(parts) > 1:
        parts.pop()
        candidate = "-".join(parts)
        if candidate in PRICES:
            return candidate
    return name


def price_for(model: str) -> tuple[float, float, float] | None:
    """($/1M input, cached input, output) for a model, or None if unknown."""
    return PRICES.get(_normalise_price_key(model))


def compute_cost_usd(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Local price-table cost for one call.

    ``prompt_tokens`` is OpenAI's total input count and already *includes*
    ``cached_tokens`` and ``cache_write_tokens`` -- they are billed at their
    own rates, so both are subtracted from the full-rate remainder rather than
    added on top. ``completion_tokens`` already includes reasoning tokens.
    Returns 0.0 for an unpriced model (never guesses).
    """
    prices = price_for(model)
    if prices is None:
        logger.warning(
            "No price entry for model %r -- its cost will be recorded as 0. "
            "Add it to PRICES in llm_provider.py.",
            model,
        )
        return 0.0
    input_rate, cached_rate, output_rate = prices
    cached = max(0, cached_tokens)
    written = max(0, cache_write_tokens)
    full_rate_input = max(0, prompt_tokens - cached - written)
    return (
        full_rate_input * input_rate
        + cached * cached_rate
        + written * input_rate * CACHE_WRITE_MULTIPLIER
        + max(0, completion_tokens) * output_rate
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def make_async_client():
    """AsyncOpenAI pointed at the active provider."""
    from openai import AsyncOpenAI  # local import: keeps openai an optional dep

    return AsyncOpenAI(base_url=base_url(), api_key=api_key())


def make_sync_client():
    """OpenAI pointed at the active provider."""
    from openai import OpenAI

    return OpenAI(base_url=base_url(), api_key=api_key())


# ---------------------------------------------------------------------------
# Request adaptation
# ---------------------------------------------------------------------------
# Pass as extra_body on OpenRouter so it returns the detailed usage breakdown
# (completion_tokens_details.reasoning_tokens, cost). OpenAI 400s on it.
OPENROUTER_USAGE_ACCOUNTING: Final[dict[str, Any]] = {"usage": {"include": True}}


def chat_kwargs(resolved_model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """DEPRECATED. Thin shim over :func:`build_kwargs`; use that instead.

    Kept so the call sites that still resolve a model before shaping its
    request keep working unchanged. It has to reverse-map the concrete id back
    to a Route, which only works while no two routes share an id with
    different policy (``_reverse_index`` logs loudly if that stops being true)
    -- which is precisely why callers should hand over the INTERNAL name and
    let ``route_for`` do the work.

    Behavioural equivalence with the pre-route implementation is pinned by
    ``tests/golden_chat_kwargs.json`` (550 request snapshots).
    """
    return build_kwargs(route_for(resolved_model), payload)


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------
# Everything above this line assumes ONE endpoint, chosen once by LLM_PROVIDER
# and frozen at import. That holds while every model lives behind a single
# base URL. It stops holding the moment two endpoints must serve one run --
# e.g. a paid Tier-1 model on OpenRouter alongside a free Tier-2 model on a
# university gateway.
#
# The obstacle is not the URL, it is the ORDER OF OPERATIONS. Every call site
# does `model = resolve_model(model)` and only then `chat_kwargs(model, ...)`,
# so the internal name -- the only thing that identifies the ROLE -- is thrown
# away before the parameter policy is chosen. With one endpoint that is
# harmless because the policy is global. With two it is wrong: the same
# concrete id can legitimately sit in two routes with different policy.
#
# So the unit that travels is a Route, not a string. A Route says where the
# call goes, what it is called there, and how the request must be shaped --
# and the shaping is keyed on the MODEL FAMILY, not on the provider. That
# distinction is the whole point: `temperature` is rejected by the GPT-5/6
# reasoning models wherever they are served, and accepted by open-weight
# models wherever they are served. Asking "is this OpenAI?" gets that wrong in
# both directions as soon as a reasoning model is reached through a broker.

from dataclasses import dataclass, field, replace  # noqa: E402
from typing import Mapping  # noqa: E402


@dataclass(frozen=True)
class Endpoint:
    """One API base URL, its credential, and its traffic limits."""

    name: str
    label: str
    base_url: str
    api_key_env: str
    requests_per_minute: float = 0.0   # 0 == ungated
    timeout_seconds: float = 600.0
    probe_on_start: bool = False


ENDPOINT_OPENROUTER: Final = Endpoint(
    "openrouter", "OpenRouter", OPENROUTER_BASE_URL, "OPENROUTER_API_KEY",
)
ENDPOINT_OPENAI: Final = Endpoint(
    "openai", "OpenAI", OPENAI_BASE_URL, "OPENAI_API_KEY",
)

# NUS SoC LLM-as-a-Service: OpenAI-compatible, open-weight models, free in
# money. Two measured facts shape this entry.
#
# RATE. The docs advertise 90 requests/minute; a real key measured 30. Tier 2
# is ~20-40 calls per question and the gate is process-wide, so this is the
# binding constraint on a run, not the token budget. 24/min leaves headroom
# for the retries that a 429 would otherwise turn into a storm.
#
# REACHABILITY. Measured 2026-09-12 from off-campus: the API host resolves to
# a PUBLIC address (137.132.84.188) and answers, while the portal and DocHub
# hosts are RFC1918 (172.18.x) and time out. So this endpoint works from
# GitHub Actions, and the portal budget endpoint the plan wanted to poll at
# run start does not -- quota has to come from the local ledger instead.
ENDPOINT_SOCLAAS: Final = Endpoint(
    "soclaas",
    "SoCLaaS",
    (os.getenv("SOCLAAS_BASE_URL") or "https://soclaas-api.comp.nus.edu.sg/v1").rstrip("/"),
    "SOCLAAS_API_KEY",
    requests_per_minute=24.0,
    # Deliberately ABOVE the gateway's own cut-off, not below it. The gateway
    # ends any request at ~300 s with a 502 (measured 2026-09-15: long xhigh
    # completions failed at 300.4 s). A client timeout under that -- this was
    # 180 s -- abandons calls that were still generating and would have
    # finished; each abandoned call was then resent from scratch (run
    # 2026-09-15_03-10 lost ~9 minutes that way). Above it, our timeout only
    # fires on a dead connection, and the gateway's 502 names the real limit.
    timeout_seconds=330.0,
    probe_on_start=True,
)


@dataclass(frozen=True)
class Route:
    """Where one logical model goes, and how its request must be shaped.

    The flags are deliberately capability-shaped rather than provider-shaped.
    ``accepts_temperature`` is a fact about the model; ``sends_usage_accounting``
    is a fact about the endpoint; both belong to the pair, which is what a
    Route is.
    """

    endpoint: Endpoint
    model_id: str

    # --- request shaping -----------------------------------------------
    accepts_temperature: bool = True
    uses_max_completion_tokens: bool = False
    reasoning_effort: str | None = None
    reasoning_effort_env_aware: bool = False
    # For models that THINK BY DEFAULT and whose thinking is not accounted as
    # reasoning (Qwen3.8 on SoCLaaS returns it in `message.reasoning`). Such a
    # model thinks through every call unless told not to, so the default here
    # is off and only calls that ask to deliberate keep it -- see build_kwargs.
    thinks_unless_disabled: bool = False
    deliberate_headroom_tokens: int = 0
    reasoning_headroom_tokens: int = 0
    max_output_tokens_ceiling: int = MAX_COMPLETION_TOKENS_CEILING
    supports_cache_control: bool = False
    sends_usage_accounting: bool = False
    prompt_cache_mode: str | None = None
    prompt_cache_env_aware: bool = False

    # --- accounting -----------------------------------------------------
    # native      = the response carries a cost field (OpenRouter)
    # price_table = compute locally from PRICES (OpenAI direct)
    # quota       = free in money, but consumes a real request/token budget
    cost_source: str = "native"
    quota_rate_microdollars: tuple[float, float] | None = None

    # --- resilience -------------------------------------------------------
    # Internal model name to reroute to when this route's endpoint is down.
    # A name, not a flag, so the fallback arrives with ITS OWN policy rather
    # than inheriting the dead route's.
    fallback: str | None = None


@dataclass(frozen=True)
class Profile:
    """A whole run's routing: every internal name, plus the forecaster pool."""

    name: str
    routes: Mapping[str, Route]
    default_forecaster: str
    forecaster_pool: tuple[str, ...]
    heterogeneous_model: str
    label_overrides: Mapping[str, str] = field(default_factory=dict)


# Internal role-carrying names the bot uses. Every profile must answer for all
# of them, so a missing route is a build-time error rather than a 404 mid-run.
INTERNAL_MODEL_NAMES: Final[tuple[str, ...]] = (
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
)


# --- Choosing models: two env vars, not a mode name -------------------------
# The role names above say WHICH JOB a call is doing; TIER1_MODEL and
# TIER2_MODEL say which concrete model does that job. Naming the models
# directly beats an opaque profile label ("mixed") on every count: it is
# self-describing, it composes (any Tier-1 with any Tier-2, no new label), and
# it does not multiply -- a third pairing needs no third mode.
#
#   TIER1_MODEL=openai/gpt-6-astra  TIER2_MODEL=qwen3.8:27b
#
# What is deliberately NOT configurable is request POLICY. accepts_temperature,
# reasoning_headroom_tokens, supports_cache_control, sends_usage_accounting and
# cost_source are facts about a model/endpoint pair, not preferences: get one
# wrong and you pay a 1.25x cache-write premium, or send a body argument the
# endpoint rejects, or have a 20k output cap eaten entirely by hidden
# reasoning. Those come from MODEL_ROUTES below, keyed on the model.
#
# Unset means unchanged: LLM_ROUTING still selects a legacy preset, and each
# role name keeps routing to itself. The overrides are a layer on top.
TIER1_ROLE: Final = "anthropic/claude-opus-5.5"
TIER2_ROLE: Final = "anthropic/claude-sonnet-5"
TIER1_ROLE_NAMES: Final[frozenset[str]] = frozenset(
    {TIER1_ROLE, "openai/gpt-5.6-sol"}
)
TIER2_ROLE_NAMES: Final[frozenset[str]] = frozenset(
    {TIER2_ROLE, "openai/gpt-5.6-terra", "openai/gpt-5.6-luna"}
)

# Microdollars per 1M tokens (input, output), from the gateway's own model
# table. No real money is charged, but the allowance is real -- measured
# $50/day against documented $80 -- so it is tracked as `quota`, never as cost.
SOCLAAS_QUOTA_RATES: Final[dict[str, tuple[float, float]]] = {
    "qwen3.8:27b": (195_000.0, 900_000.0),
    "qwen3.6:35b": (140_000.0, 900_000.0),
    "gemma4:26b": (60_000.0, 300_000.0),
    "llama3.1:8b": (20_000.0, 30_000.0),
}

# Where a SoCLaaS route falls back when the gateway is down. An internal NAME,
# so the fallback arrives with its own policy, and one that must resolve to a
# route on a DIFFERENT endpoint -- a fallback onto the endpoint that just
# failed is not a fallback. preflight() asserts exactly that.
_QUOTA_FALLBACK_ROLE: Final = "openai/gpt-5.6-terra"


def _openrouter_route(model: str) -> Route:
    """OpenRouter serves every vendor behind one URL and reports real cost."""
    return Route(
        endpoint=ENDPOINT_OPENROUTER,
        model_id=model,
        accepts_temperature=True,
        uses_max_completion_tokens=False,
        supports_cache_control=True,
        sends_usage_accounting=True,
        cost_source="native",
    )


def _openai_route(internal_name: str) -> Route:
    """OpenAI direct: reasoning models, local pricing, no usage extension.

    ``*_env_aware`` defer to :func:`reasoning_effort_for` and
    :func:`prompt_cache_options_for` at request time, because both read env
    vars that a caller may change after import -- baking their values into a
    frozen Route here would quietly break the documented overrides.
    """
    return Route(
        endpoint=ENDPOINT_OPENAI,
        model_id=MODEL_TRANSLATION.get(internal_name, internal_name.split("/")[-1]),
        accepts_temperature=False,
        uses_max_completion_tokens=True,
        reasoning_effort_env_aware=True,
        supports_cache_control=False,
        sends_usage_accounting=False,
        prompt_cache_env_aware=True,
        cost_source="price_table",
    )


def _gpt6_openrouter_route(model_id: str) -> Route:
    """A GPT-6-family reasoning model reached through OpenRouter.

    ``supports_cache_control=False`` is load-bearing rather than cosmetic.
    OpenRouter forwards Anthropic-style cache_control breakpoints, and the
    binary tiebreaker is the one call site that still sets
    cache_static_prefix=True. Against an OpenAI-family model those breakpoints
    buy nothing, and a cache WRITE on Astra bills at 1.25x input ($12.50/1M) --
    a premium on a prompt that is unique to one question and can never be read
    back.

    ``reasoning_headroom_tokens`` exists because max_tokens on a reasoning
    model caps visible output AND hidden reasoning together. Without it the
    20k forecast cap could be consumed entirely by reasoning, returning an
    empty message with finish_reason=length.
    """
    return Route(
        endpoint=ENDPOINT_OPENROUTER,
        model_id=model_id,
        accepts_temperature=True,      # measured live: 5/5 distinct completions
        uses_max_completion_tokens=False,
        reasoning_headroom_tokens=24_000,
        supports_cache_control=False,
        sends_usage_accounting=True,
        cost_source="native",
    )


def _soclaas_route(model_id: str) -> Route:
    """An open-weight model on the gateway: free in money, metered in quota.

    THINKING IS OFF UNLESS A CALL DELIBERATES. Qwen3.8 thinks by default, at
    ~70 tok/s on the gateway. Run 2026-09-15_03-10 (Q45707) never left
    research: tavily-url-ranking spent 124 s producing 8,815 tokens for a JSON
    list, three calls hit the 180 s timeout and were retried from scratch, and
    page-summary spent its whole 3,500-token cap thinking and returned no
    content. The question hit the 20-minute limit. Measured on the same
    ranking prompt with `reasoning_effort: "none"`: 14.6 s, 896 tokens, valid
    JSON -- though it selected 8 URLs where thinking selected 17, all 8 among
    the 17. Research plumbing takes that trade; forecasts do not, so forecast
    and tiebreaker calls pass deliberate=True and keep thinking.
    """
    return Route(
        endpoint=ENDPOINT_SOCLAAS,
        model_id=model_id,
        accepts_temperature=True,      # open-weight model; verified accepted
        uses_max_completion_tokens=False,
        thinks_unless_disabled=True,
        deliberate_headroom_tokens=16_000,
        supports_cache_control=False,
        # The gateway is OpenAI-compatible and has no OpenRouter usage
        # extension; sending one would be an unrecognised body argument.
        sends_usage_accounting=False,
        cost_source="quota",
        quota_rate_microdollars=SOCLAAS_QUOTA_RATES.get(model_id),
        fallback=_QUOTA_FALLBACK_ROLE,
    )


def route_for_model(model_id: str) -> Route:
    """Concrete model id -> how to reach it. The registry the tiers select from.

    Recognised by shape rather than by an exhaustive list, so a sibling model
    (gpt-6-astra-pro, qwen3.6:35b) works without an edit here. An unrecognised
    id falls through to OpenRouter, which is the one endpoint that multiplexes
    every vendor -- the safest guess, and it is logged.
    """
    name = model_id.strip()
    if name in SOCLAAS_QUOTA_RATES or ":" in name:
        # `family:size` is the gateway's id convention and nobody else's.
        return _soclaas_route(name)
    if "gpt-6" in name or "gpt-5" in name:
        return _gpt6_openrouter_route(name)
    return _openrouter_route(name)


def _tier_overrides() -> dict[str, Route]:
    """Role name -> Route, for whichever tiers TIER1/TIER2_MODEL name.

    Returns {} when neither is set, which is what keeps the legacy presets
    byte-identical and the golden request snapshots valid.
    """
    overrides: dict[str, Route] = {}
    for env_var, role_names in (
        ("TIER1_MODEL", TIER1_ROLE_NAMES),
        ("TIER2_MODEL", TIER2_ROLE_NAMES),
    ):
        chosen = (os.getenv(env_var) or "").strip()
        if not chosen:
            continue
        route = route_for_model(chosen)
        for role in role_names:
            # The quota fallback role must keep a route on a paid endpoint, or
            # the fallback it provides is circular. Leave it alone.
            if role == _QUOTA_FALLBACK_ROLE and route.cost_source == "quota":
                continue
            overrides[role] = route
        logger.info(
            "[routing] %s=%s -> %s @ %s",
            env_var, chosen, route.model_id, route.endpoint.label,
        )
    return overrides


def _apply_tier_choice(profile: Profile) -> Profile:
    """Fold TIER1/TIER2_MODEL into a preset: routes AND the ensemble.

    Deriving the pool from the tiers is what keeps this to two variables
    instead of four. Naming a Tier 2 model is a statement about the whole run,
    so the ensemble follows it: the pool becomes one model of each tier and the
    raw-research member becomes the Tier 2 one, which is the cheaper and more
    decorrelated arrangement (two lineages over three inputs) rather than two
    identical Tier 1 runs on one brief.

    FORECASTER_MODELS still overrides this in config.py, so an unusual pool
    stays expressible.
    """
    overrides = _tier_overrides()
    if not overrides:
        return profile
    return replace(
        profile,
        routes=dict(profile.routes) | overrides,
        forecaster_pool=(TIER1_ROLE, TIER2_ROLE),
        heterogeneous_model=TIER2_ROLE,
    )


def _openrouter_profile() -> Profile:
    return _apply_tier_choice(Profile(
        name=PROVIDER_OPENROUTER,
        routes={name: _openrouter_route(name) for name in INTERNAL_MODEL_NAMES},
        default_forecaster="anthropic/claude-opus-5.5",
        forecaster_pool=("anthropic/claude-opus-5.5", "openai/gpt-6-sol"),
        heterogeneous_model="anthropic/claude-sonnet-5",
    ))


def _openai_profile() -> Profile:
    # The pool is a capability ladder, not a lineage mix: MODEL_TRANSLATION
    # maps BOTH anthropic/claude-opus-5.5 and openai/gpt-5.6-sol onto
    # gpt-5.6-sol, so a lineage pool would make runs 1 and 2 the same model on
    # the same brief -- and temperature is dropped here, so they could not even
    # be decorrelated by sampling. Accepted cost of a single-vendor setup.
    return _apply_tier_choice(Profile(
        name=PROVIDER_OPENAI,
        routes={name: _openai_route(name) for name in INTERNAL_MODEL_NAMES},
        default_forecaster="gpt-5.6-sol",
        forecaster_pool=("gpt-5.6-sol", "gpt-5.6-terra"),
        heterogeneous_model="gpt-5.6-terra",
    ))


_PROFILE_BUILDERS: Final[dict[str, Any]] = {
    PROVIDER_OPENROUTER: _openrouter_profile,
    PROVIDER_OPENAI: _openai_profile,
}

_PROFILE: Profile | None = None
_ROUTE_CACHE: dict[tuple[str, str | None], Route] = {}


def _read_routing_mode() -> str:
    """The active mode. Single source of truth: the module-level constant.

    Deriving it here rather than re-reading the environment is what keeps the
    legacy helpers (is_openai, api_key, base_url) and the route table from
    drifting apart -- they would otherwise disagree the moment someone set
    LLM_ROUTING without also setting LLM_PROVIDER.
    """
    return LLM_PROVIDER


def active_profile() -> Profile:
    """The routing profile for this process. Memoised; see reset_routing()."""
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = _PROFILE_BUILDERS[_read_routing_mode()]()
    return _PROFILE


def reset_routing() -> None:
    """Drop the memoised profile and route cache. For tests and preflight."""
    global _PROFILE
    _PROFILE = None
    _ROUTE_CACHE.clear()


def _reverse_index(profile: Profile) -> dict[str, Route]:
    """Concrete ``model_id`` -> Route, for callers that resolved early.

    Several internal names can share one concrete id (under the OpenAI profile
    both ``anthropic/claude-opus-5.5`` and ``openai/gpt-5.6-sol`` become
    ``gpt-5.6-sol``). That is fine while they share a policy; it is a genuine
    ambiguity if they ever do not, so say so loudly rather than picking one.
    """
    index: dict[str, Route] = {}
    for route in profile.routes.values():
        seen = index.get(route.model_id)
        if seen is None:
            index[route.model_id] = route
        elif seen != route:
            logger.error(
                "Two routes claim model id %r with different policy (%s vs %s). "
                "A caller that resolves the model before routing will get an "
                "arbitrary one of them.",
                route.model_id, seen, route,
            )
    return index


def _fallback_route(model: str) -> Route:
    """A name no profile claims -- resolve it as a CONCRETE model id.

    This is the path that makes listing real models work anywhere a model is
    named: FORECASTER_MODELS=openai/gpt-6-astra,qwen3.8:27b reaches two
    different endpoints with two different request policies, without either
    name having to be a role in INTERNAL_MODEL_NAMES.

    It must not fall back to "the profile's own endpoint", which was the
    previous behaviour: under the OpenRouter preset that would have sent
    `qwen3.8:27b` to OpenRouter carrying OpenRouter's flags -- a 404 at best,
    and at worst a request shaped for the wrong endpoint. route_for_model
    recognises the model instead, so the endpoint follows the model."""
    route = route_for_model(model)
    logger.info(
        "[routing] %r is not a role name; resolved as a concrete model -> %s @ %s",
        model, route.model_id, route.endpoint.label,
    )
    return route


# --- Research tasks on Qwen: one env var, by call-site label ----------------
# QWEN_RESEARCH_LABELS moves individual research call sites to SoCLaaS Qwen
# without touching TIER2_MODEL, which would also move the forecaster ensemble.
# A comma list of labels; an entry ending in "/" matches every label under it
# ("kalshi/"). "tested" names every research task the SoCLaaS quality
# investigation evaluated (docs/diagnostics, consolidated report section 5.3);
# "recommended" is the same minus the evidence plan (see below).
# The Wikipedia adapter is the one research task it never tested, so it is
# deliberately absent. Unset means routing is unchanged.
QWEN_RESEARCH_MODEL: Final = "qwen3.8:27b"
QWEN_TESTED_RESEARCH_LABELS: Final[tuple[str, ...]] = (
    "asknews-filter",
    "evidence-plan",
    "artifact-check",
    "google-query-generation",
    "serp-url-ranking",
    "tavily-url-ranking",
    "firecrawl-url-ranking",
    "serp-scrape-extract",
    "kalshi/",
    "manifold/",
    "polymarket/",
    "resolution-scraper/page-summary",
    "resolution-scraper/wayback-history",
    "compiler/precompress",
    "compiler/research-brief",
)


# "recommended" keeps the evidence plan on its paid model. In the 2026-09-21
# research A/B, Qwen's plan made a background example the research target
# (45707) and every later search followed it; the plan costs ~$0.04 a question.
QWEN_RECOMMENDED_RESEARCH_LABELS: Final[tuple[str, ...]] = tuple(
    label for label in QWEN_TESTED_RESEARCH_LABELS if label != "evidence-plan"
)


def qwen_research_labels() -> tuple[str, ...]:
    """The label patterns QWEN_RESEARCH_LABELS selects. Read live."""
    raw = (os.getenv("QWEN_RESEARCH_LABELS") or "").strip()
    if not raw:
        return ()
    if raw.lower() == "tested":
        return QWEN_TESTED_RESEARCH_LABELS
    if raw.lower() == "recommended":
        return QWEN_RECOMMENDED_RESEARCH_LABELS
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def _qwen_label_override(label: str | None) -> str | None:
    if not label:
        return None
    for pattern in qwen_research_labels():
        if label == pattern or (pattern.endswith("/") and label.startswith(pattern)):
            return QWEN_RESEARCH_MODEL
    return None


def route_for(model: str, *, label: str | None = None) -> Route:
    """Internal role name (or a concrete id) -> Route. Never raises.

    ``label`` is the per-call-site tag already threaded to every call
    (``"serp/ranking"``, ``"compiler/research-brief"``, ...). It lets a profile
    send one call site somewhere different without touching that call site --
    the dial needed when only *part* of a tier should move to a new endpoint.
    """
    key = (model, label)
    hit = _ROUTE_CACHE.get(key)
    if hit is not None:
        return hit
    profile = active_profile()
    name = (
        profile.label_overrides.get(label or "")
        or _qwen_label_override(label)
        or model
    )
    route = (
        profile.routes.get(name)
        or _reverse_index(profile).get(name)
        or _fallback_route(name)
    )
    _ROUTE_CACHE[key] = route
    return route


def api_key_for(endpoint: Endpoint) -> str:
    """The key for one endpoint ("" when unset). Read live, never cached."""
    return (os.getenv(endpoint.api_key_env) or "").strip()


def required_endpoints() -> list[Endpoint]:
    """Every endpoint this profile can reach, including fallback targets.

    Startup validation uses this so a missing key fails before the first
    question rather than forty calls in.
    """
    profile = active_profile()
    seen: dict[str, Endpoint] = {}
    for route in profile.routes.values():
        seen.setdefault(route.endpoint.name, route.endpoint)
        if route.fallback:
            target = profile.routes.get(route.fallback)
            if target is not None:
                seen.setdefault(target.endpoint.name, target.endpoint)
    return list(seen.values())


def preflight() -> list[str]:
    """Problems that would break the active profile, found before question 1.

    Key presence and table consistency only -- deliberately no network. A
    startup probe would add a failure mode of its own (a transient blip
    refusing to start a run that would have succeeded), and unreachability is
    already handled at request time by the fallback route.

    Returns a list of human-readable problems; empty means good to go.
    """
    problems: list[str] = []
    profile = active_profile()

    # 1. Every role the bot names must have somewhere to go. A missing route
    #    would otherwise surface as a 404 partway through a question.
    for name in INTERNAL_MODEL_NAMES:
        if name not in profile.routes:
            problems.append(
                f"routing profile {profile.name!r} has no route for {name!r}"
            )

    # 2. Every endpoint it can reach -- including fallback targets -- needs its
    #    credential. Checking here turns forty confusing 401s into one message.
    for endpoint in required_endpoints():
        if not api_key_for(endpoint):
            problems.append(
                f"{endpoint.label} is routed to but {endpoint.api_key_env} is not set"
            )

    # 3. A fallback that lands on the endpoint that just failed is not a
    #    fallback. Cheap to assert, and the mistake is easy to make because
    #    the fallback is written as a role name rather than an endpoint.
    for name, route in profile.routes.items():
        if not route.fallback:
            continue
        target = profile.routes.get(route.fallback)
        if target is None:
            problems.append(
                f"{name!r} falls back to {route.fallback!r}, which has no route"
            )
        elif target.endpoint.name == route.endpoint.name:
            problems.append(
                f"{name!r} falls back to {route.fallback!r}, but both are on "
                f"{route.endpoint.label} -- a fallback must cross endpoints"
            )
    return problems


def build_kwargs(
    route: Route, payload: dict[str, Any], *, deliberate: bool = False
) -> dict[str, Any]:
    """Complete kwargs for ``chat.completions.create``, shaped for one Route.

    Supersedes :func:`chat_kwargs`, which could only ask "is this OpenAI?".
    ``deliberate`` marks a call whose answer IS the reasoning (a forecast), as
    opposed to plumbing (ranking, extraction, summaries). It only changes
    anything on a route with ``thinks_unless_disabled``.
    """
    kwargs: dict[str, Any] = {"model": route.model_id, **payload}

    if not route.accepts_temperature:
        kwargs.pop("temperature", None)

    # extra_body is ASSEMBLED here, never inherited from the caller. The old
    # chat_kwargs popped the caller's copy and then overwrote it with
    # prompt_cache_options -- with one more field those two would have
    # silently excluded each other.
    kwargs.pop("extra_body", None)
    extra: dict[str, Any] = {}
    if route.sends_usage_accounting:
        extra.update(OPENROUTER_USAGE_ACCOUNTING)
    if route.prompt_cache_env_aware:
        options = prompt_cache_options_for(route.model_id)
        if options is not None:
            extra["prompt_cache_options"] = options
    elif route.prompt_cache_mode:
        extra["prompt_cache_options"] = {"mode": route.prompt_cache_mode}
    if extra:
        kwargs["extra_body"] = extra

    effort: str | None = None
    if route.reasoning_effort_env_aware:
        effort = reasoning_effort_for(route.model_id)
    elif route.reasoning_effort:
        effort = route.reasoning_effort
    if effort:
        kwargs["reasoning_effort"] = effort
    if route.thinks_unless_disabled and not deliberate:
        kwargs["reasoning_effort"] = "none"

    visible = kwargs.pop("max_tokens", None)
    if visible is not None:
        if route.reasoning_effort_env_aware:
            grown = max_completion_tokens_for(
                int(visible), effort or DEFAULT_REASONING_EFFORT
            )
        elif deliberate and route.thinks_unless_disabled:
            # max_tokens caps thinking and answer together, so a thinking
            # call needs the same headroom a reasoning model gets.
            grown = min(
                route.max_output_tokens_ceiling,
                max(1, int(visible)) + route.deliberate_headroom_tokens,
            )
        elif route.reasoning_headroom_tokens or route.uses_max_completion_tokens:
            grown = min(
                route.max_output_tokens_ceiling,
                max(1, int(visible)) + route.reasoning_headroom_tokens,
            )
        else:
            # No headroom and a plain max_tokens field: pass the caller's value
            # through untouched rather than clamping it to a ceiling it never
            # had before.
            grown = visible
        kwargs["max_completion_tokens" if route.uses_max_completion_tokens
               else "max_tokens"] = grown

    return kwargs


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
# Cached per endpoint. The old make_async_client() built a FRESH AsyncOpenAI on
# every call, so each of the ~40 LLM calls in a question opened its own
# connection pool and none of the TLS handshakes were reused.
#
# max_retries=0 is not a detail. The OpenAI SDK retries twice by default, and
# it does so INSIDE chat.completions.create -- underneath llm_client's retry
# loop and underneath any rate gate. On a rate-limited endpoint that silently
# triples the request rate at exactly the moment it must fall. Retry belongs to
# llm_client, which knows about the gate; the SDK must not have its own.

_async_clients: dict[tuple[str, int], Any] = {}
_sync_clients: dict[str, Any] = {}
_client_lock = threading.Lock()


def _ladder_client(route: Route, sync: bool):
    """The XHigh-then-Medium client, when QWEN_LADDER=1 and the route is SoCLaaS."""
    if route.endpoint.name != ENDPOINT_SOCLAAS.name:
        return None
    import qwen_ladder  # local import: qwen_ladder imports this module

    if not qwen_ladder.enabled():
        return None
    return qwen_ladder.SyncLadderClient(route) if sync else qwen_ladder.AsyncLadderClient(route)


def async_client_for(route: Route):
    """Cached AsyncOpenAI for this route's endpoint."""
    from openai import AsyncOpenAI  # local import: keeps openai an optional dep

    ladder = _ladder_client(route, sync=False)
    if ladder is not None:
        return ladder
    endpoint = route.endpoint
    # Keyed by event loop as well: an AsyncOpenAI binds its httpx pool to the
    # loop that created it. Production runs one asyncio.run(), but eval_tools
    # and any future test harness make their own.
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (endpoint.name, loop_id)
    client = _async_clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=endpoint.base_url,
            api_key=api_key_for(endpoint),
            timeout=endpoint.timeout_seconds,
            max_retries=0,
        )
        _async_clients[key] = client
    return client


def sync_client_for(route: Route):
    """Cached OpenAI client for this route's endpoint.

    Lock-guarded because the prediction-market providers call this from
    ``asyncio.to_thread`` worker threads.
    """
    from openai import OpenAI

    ladder = _ladder_client(route, sync=True)
    if ladder is not None:
        return ladder
    endpoint = route.endpoint
    with _client_lock:
        client = _sync_clients.get(endpoint.name)
        if client is None:
            client = OpenAI(
                base_url=endpoint.base_url,
                api_key=api_key_for(endpoint),
                timeout=endpoint.timeout_seconds,
                max_retries=0,
            )
            _sync_clients[endpoint.name] = client
        return client


def reset_clients() -> None:
    """Drop cached clients. For tests, and after a credential/route change."""
    _async_clients.clear()
    with _client_lock:
        _sync_clients.clear()


# ---------------------------------------------------------------------------
# Rate gating
# ---------------------------------------------------------------------------

class RateGate:
    """Process-wide request spacer, usable from the event loop AND threads.

    Reserve-then-sleep: the slot is claimed atomically under a lock held for
    microseconds, then the caller sleeps outside the lock in whichever world it
    lives in. N concurrent waiters therefore get N *distinct, spaced* slots.
    The naive alternative -- sleep until ``next_at``, then advance it -- wakes
    every waiter at the same instant and turns a 30/min limiter into a
    30-request burst.

    A gate with ``requests_per_minute <= 0`` is a no-op, which is what the
    OpenRouter and OpenAI endpoints use.
    """

    def __init__(self, requests_per_minute: float, name: str = "") -> None:
        self.name = name
        self.requests_per_minute = requests_per_minute
        self._interval = 0.0 if requests_per_minute <= 0 else 60.0 / requests_per_minute
        self._lock = threading.Lock()
        self._next_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._interval > 0.0

    def _reserve(self) -> float:
        if not self.enabled:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self._interval
            return start - now

    async def wait_async(self) -> None:
        delay = self._reserve()
        if delay > 0:
            logger.debug("[rate] %s: waiting %.2fs for a slot", self.name, delay)
            await asyncio.sleep(delay)

    def wait_sync(self) -> None:
        delay = self._reserve()
        if delay > 0:
            logger.debug("[rate] %s: waiting %.2fs for a slot", self.name, delay)
            time.sleep(delay)

    def penalise(self, seconds: float) -> None:
        """A 429 arrived: push the whole queue back, not just this caller."""
        if seconds <= 0:
            return
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)
        logger.warning("[rate] %s: backing off %.1fs after a rate-limit response",
                       self.name, seconds)


_NULL_GATE: Final = RateGate(0.0, "ungated")
_GATES: dict[str, RateGate] = {}


def gate_for(route: Route) -> RateGate:
    """The shared gate for this route's endpoint (a no-op when ungated)."""
    endpoint = route.endpoint
    if endpoint.requests_per_minute <= 0:
        return _NULL_GATE
    gate = _GATES.get(endpoint.name)
    if gate is None:
        gate = RateGate(endpoint.requests_per_minute, endpoint.name)
        _GATES[endpoint.name] = gate
    return gate


def _parse_retry_after(exc: Any) -> float | None:
    """Seconds from a Retry-After header, when the provider sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # noqa: BLE001 - header containers vary by transport
        return None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# A 429 can mean two different things on a budgeted endpoint, and they need
# opposite responses: "too fast" wants a backoff and a retry, "out of budget"
# wants neither -- retrying a daily-quota rejection just burns request slots.
_QUOTA_MARKERS: Final = ("quota", "budget", "daily", "monthly", "credit", "exhaust")


def is_quota_exhaustion(exc: Any) -> bool:
    """True when a 429 looks like a spent budget rather than a burst."""
    if getattr(exc, "status_code", None) != 429:
        return False
    body = str(getattr(getattr(exc, "response", None), "text", "") or exc).lower()
    return any(marker in body for marker in _QUOTA_MARKERS)


def note_failure(route: Route, exc: Any) -> None:
    """Feed a failed call back into the gate. Never raises."""
    try:
        if getattr(exc, "status_code", None) != 429:
            return
        gate = gate_for(route)
        if not gate.enabled:
            return
        if is_quota_exhaustion(exc):
            logger.error(
                "%s reports its budget exhausted -- backing off hard. Retrying "
                "a spent quota only burns request slots.", route.endpoint.label,
            )
            gate.penalise(60.0)
            return
        gate.penalise(_parse_retry_after(exc) or gate._interval * 2)
    except Exception:  # noqa: BLE001 - feedback must never sink a call
        logger.debug("note_failure failed for %s", route.model_id, exc_info=True)


# ---------------------------------------------------------------------------
# One-call helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prepared:
    """A routed, shaped request ready to hand to a client."""

    route: Route
    kwargs: dict[str, Any]

    @property
    def model_id(self) -> str:
        return self.route.model_id


def prepare(model: str, payload: dict[str, Any], *, label: str | None = None) -> Prepared:
    """Route ``model`` and shape ``payload`` for wherever it lands."""
    route = route_for(model, label=label)
    return Prepared(route, build_kwargs(route, payload))


def sync_chat(label: str, model: str, payload: dict[str, Any]):
    """One blocking chat completion: routed, gated, and cost-accounted.

    Safe from an ``asyncio.to_thread`` worker -- the MonetaryCostManager
    ContextVar is copied into the thread, so records land in the right ledger.

    This exists because the three prediction-market providers are synchronous
    and each carried its own copy of client construction, model resolution and
    usage recording. Being sync, they also bypass the asyncio semaphore
    entirely, which is exactly why the gate has to be acquired here too.
    """
    from monetary_cost_manager import MonetaryCostManager

    prepared = prepare(model, payload, label=label)
    usage_handle = MonetaryCostManager.start_openrouter_call(
        label, prepared.model_id, payload
    )
    gate_for(prepared.route).wait_sync()
    response = sync_client_for(prepared.route).chat.completions.create(**prepared.kwargs)
    usage_handle.record_response(response)
    return response
