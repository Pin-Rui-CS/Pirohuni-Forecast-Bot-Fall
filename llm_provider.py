"""Single point of divergence between the OpenRouter and the direct OpenAI API.

Everything that differs between the two providers lives here: base URLs, API
keys, model names, request parameters, and per-call pricing. The rest of the
bot keeps calling ``chat.completions.create`` with the same logical payload it
always did; this module rewrites that payload for whichever provider is active.

Flip providers with one env var::

    LLM_PROVIDER=openrouter   # default -- unchanged behaviour
    LLM_PROVIDER=openai       # direct OpenAI API

Why a translation layer instead of editing every call site: the bot names its
models in 17 places across 13 files (``anthropic/claude-opus-5`` for the
forecaster/compiler/tiebreaker roles, ``anthropic/claude-sonnet-5`` for the
research utility roles). ``resolve_model`` maps those role-carrying names onto
the OpenAI equivalents at the client boundary, so switching providers touches
no call site and reverting is a single env var.

The four differences that would otherwise fail hard on the OpenAI API:

1. ``temperature`` is rejected outright by the GPT-5 reasoning models
   (400 unsupported_value -- only the default 1 is allowed). OpenRouter
   silently normalises it away, which is why the existing gpt-5.6-sol ensemble
   member works today. We drop it.
2. ``extra_body={"usage": {"include": True}}`` is an OpenRouter extension;
   OpenAI 400s on unrecognised body arguments. We drop it and price the call
   locally instead (see PRICES).
3. ``max_tokens`` is replaced by ``max_completion_tokens``, and that cap now
   also has to cover the model's internal reasoning tokens. A cap sized for
   the visible answer alone can be consumed entirely by reasoning, returning
   an empty message with finish_reason=length. We add per-effort headroom.
4. Anthropic-style ``cache_control`` breakpoints in message content parts are
   meaningless here; OpenAI caches automatically. See PROMPT_CACHE_MODE for
   why we turn that off rather than leave it on.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Final

import dotenv

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

PROVIDER_OPENROUTER: Final = "openrouter"
PROVIDER_OPENAI: Final = "openai"
_VALID_PROVIDERS: Final = frozenset({PROVIDER_OPENROUTER, PROVIDER_OPENAI})


def _read_provider() -> str:
    raw = (os.getenv("LLM_PROVIDER") or PROVIDER_OPENROUTER).strip().lower()
    if raw not in _VALID_PROVIDERS:
        logger.warning(
            "LLM_PROVIDER=%r is not one of %s -- falling back to %r.",
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
# rather than a step change in spend.
MODEL_TRANSLATION: Final[dict[str, str]] = {
    "anthropic/claude-opus-5": "gpt-5.6-sol",
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
    """Complete kwargs for ``chat.completions.create``, adapted to the provider.

    ``payload`` is the logical request the bot has always built (messages,
    temperature, max_tokens, stream, tools, ...). ``resolved_model`` must
    already have been through :func:`resolve_model`.

    On OpenRouter this is a passthrough plus usage accounting. On OpenAI:
    temperature is dropped, max_tokens becomes max_completion_tokens with
    reasoning headroom, and reasoning-effort / prompt-cache controls are added.
    """
    kwargs: dict[str, Any] = {"model": resolved_model, **payload}

    if not is_openai():
        kwargs["extra_body"] = OPENROUTER_USAGE_ACCOUNTING
        return kwargs

    # 1. temperature is rejected by the GPT-5 reasoning models.
    kwargs.pop("temperature", None)
    # 2. no OpenRouter usage extension; cost is computed locally.
    kwargs.pop("extra_body", None)

    effort = reasoning_effort_for(resolved_model)
    kwargs["reasoning_effort"] = effort

    # 3. max_tokens -> max_completion_tokens, grown to cover reasoning.
    visible_max_tokens = kwargs.pop("max_tokens", None)
    if visible_max_tokens is not None:
        kwargs["max_completion_tokens"] = max_completion_tokens_for(
            int(visible_max_tokens), effort
        )

    # 4. avoid paying the 1.25x cache-write premium on single-use prompts.
    cache_options = prompt_cache_options_for(resolved_model)
    if cache_options is not None:
        kwargs["extra_body"] = {"prompt_cache_options": cache_options}

    return kwargs
