"""SoCLaaS gateway models: what the bot knows about each, and which to use.

Every model-specific fact the bot needs to talk to a SoCLaaS model lives in
SOCLAAS_MODELS. Which models it uses, in order, is SOCLAAS_MODEL_CHAIN:

    SOCLAAS_MODEL_CHAIN="x-test-2,qwen3.8:27b"   # DeepSeek first, Qwen if it fails

Callers ask for the role name ``"soclaas"`` (SOCLAAS_ROLE) rather than a
model id; it resolves to the head of the chain, and the request ladder
(qwen_ladder) moves down the chain when a model fails. There is never a paid
fallback: when every model in the chain fails, the call fails.

To move to a new SoCLaaS model:
  1. `python -m bot.llm.soclaas_probe <model-id>` -- one tiny free call per
     effort; prints the name the gateway answers with, the efforts it accepts,
     and its quota rates.
  2. Add a SoclaasModel entry below from that output.
  3. Put the id first in SOCLAAS_MODEL_CHAIN (the default below, or the env var
     the workflow sets).

This module imports nothing from the rest of the bot, so provider.py can use it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Final


@dataclass(frozen=True)
class SoclaasModel:
    model_id: str
    # Model names the gateway may put in a reply. The id itself always counts;
    # list any other name it answers with (DeepSeek answers "x-test-2" requests
    # as "deepseek-v4.1-flash-1017"). A reply naming any other model is refused.
    reply_names: tuple[str, ...] = ()
    # reasoning_effort ladder, deepest first. The ladder starts at the first
    # rung ("deep") or the last ("light") and steps down on a failed attempt.
    efforts: tuple[str, ...] = ("xhigh", "medium")
    # reasoning_effort for a non-deliberate call made WITHOUT the ladder
    # (plumbing: ranking, extraction). "none" switches Qwen's thinking off.
    no_think_effort: str = "none"
    # Sampling parameters sent on every ladder attempt.
    sampler: dict = field(default_factory=lambda: {"temperature": 1, "top_p": .95, "top_k": 20})
    # Microdollars per 1M (input, output) tokens, from GET /v1/models. No money
    # is charged, but the allowance is real, so it is tracked as quota.
    quota_rate: tuple[float, float] = (0.0, 0.0)
    note: str = ""

    def accepts_reply(self, name: str) -> bool:
        return name == self.model_id or name in self.reply_names


# Rates read from GET https://soclaas-api.comp.nus.edu.sg/v1/models on 2026-10-20.
SOCLAAS_MODELS: Final[dict[str, SoclaasModel]] = {m.model_id: m for m in (
    SoclaasModel(
        "x-test-2",
        reply_names=("deepseek-v4.1-flash-1017",),
        # The gateway rejects "medium" for DeepSeek V4.1 ("must be low, high,
        # xhigh, max, or an integer within [1, 100]"; 2026-10-20). It accepts
        # "none" (probe, 2026-10-22), but whether that stops thinking is
        # unverified, so plumbing calls use "low", the lightest tested rung.
        efforts=("high", "low"),
        no_think_effort="low",
        # Validated with temperature 1 / top_p 0.95 (data/soclaas-model-
        # comparison); top_k is left out rather than sent untested.
        sampler={"temperature": 1, "top_p": .95},
        quota_rate=(245_125.0, 930_500.0),  # 2026-10-22
        note="DeepSeek V4.1 Flash, labelled 'Testing' on SoCLaaS; 1M-token context",
    ),
    SoclaasModel(
        "qwen3.8:27b",
        efforts=("xhigh", "medium"),
        quota_rate=(404_351.0, 2_365_332.0),
        note="Qwen 3.8 27B; the bot's SoCLaaS model until 2026-10",
    ),
    # Listed for routing and quota accounting. Their efforts are the Qwen
    # defaults and have NOT been verified on the gateway -- probe before use.
    SoclaasModel("qwen3.6:35b", quota_rate=(411_810.0, 1_456_571.0), note="efforts unverified"),
    SoclaasModel("gemma4:26b", quota_rate=(102_059.0, 378_588.0), note="efforts unverified"),
    SoclaasModel("llama3.1:8b", quota_rate=(74_250.0, 86_750.0), note="efforts unverified"),
)}

# What callers ask for when they mean "the bot's SoCLaaS model".
SOCLAAS_ROLE: Final = "soclaas"
DEFAULT_CHAIN: Final = "x-test-2,qwen3.8:27b"

# Starting rungs a label may ask for. "medium" predates per-model ladders and
# is read as "light" for a model whose ladder has no medium rung.
DEEP: Final = "deep"
LIGHT: Final = "light"


class UnknownSoclaasModel(ValueError):
    """A model id the registry has no entry for."""


def get(model_id: str) -> SoclaasModel:
    try:
        return SOCLAAS_MODELS[model_id]
    except KeyError:
        raise UnknownSoclaasModel(
            f"SoCLaaS model {model_id!r} is not in SOCLAAS_MODELS (bot/llm/soclaas_models.py); "
            f"probe it with `python -m bot.llm.soclaas_probe {model_id}` and add an entry"
        ) from None


def lookup(model_id: str) -> SoclaasModel | None:
    return SOCLAAS_MODELS.get(model_id)


def chain() -> tuple[str, ...]:
    """SOCLAAS_MODEL_CHAIN as model ids, in the order they are tried. Read live."""
    raw = (os.getenv("SOCLAAS_MODEL_CHAIN") or "").strip() or DEFAULT_CHAIN
    ids = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not ids:
        raise UnknownSoclaasModel("SOCLAAS_MODEL_CHAIN is empty")
    return ids


def primary() -> str:
    """The model SOCLAAS_ROLE resolves to: the head of the chain."""
    return chain()[0]


def chain_problems() -> list[str]:
    """Why the configured chain cannot be used ([] when it can)."""
    try:
        ids = chain()
    except UnknownSoclaasModel as exc:
        return [str(exc)]
    return [str(UnknownSoclaasModel(f"SOCLAAS_MODEL_CHAIN names {mid!r}, which is not in SOCLAAS_MODELS"))
            for mid in ids if mid not in SOCLAAS_MODELS]


def fallbacks_from(model_id: str) -> tuple[str, ...]:
    """The models to try for a request to ``model_id``, in order.

    A model in the chain falls back to the models after it. A model outside
    the chain (a forecaster pinned to "qwen3.8:27b") is tried alone.
    """
    ids = chain()
    if model_id in ids:
        return ids[ids.index(model_id):]
    return (model_id,)


def model_for_reply(name: str) -> SoclaasModel | None:
    """The registry entry whose id or reply names include ``name``."""
    for model in SOCLAAS_MODELS.values():
        if model.accepts_reply(name):
            return model
    return None


def effort_ladder(model_id: str, start: str | None = None) -> tuple[str, ...]:
    """The rungs for one model from a starting point down to its lightest.

    ``start`` is "deep" (default), "light", or an effort name. An effort the
    model does not support is an error, except "medium", which predates
    per-model ladders and means "light" where the model has no medium rung.
    """
    efforts = get(model_id).efforts
    wanted = (start or DEEP).strip().lower()
    if wanted == DEEP:
        return efforts
    if wanted == LIGHT or (wanted == "medium" and "medium" not in efforts):
        return efforts[-1:]
    if wanted not in efforts:
        raise ValueError(f"{model_id!r} supports reasoning efforts {efforts}, not {wanted!r}")
    return efforts[efforts.index(wanted):]


def quota_microdollars(model_id_or_reply: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Quota one call spent, priced at the rate of the model that served it."""
    model = lookup(model_id_or_reply) or model_for_reply(model_id_or_reply)
    if model is None:
        return 0.0
    in_rate, out_rate = model.quota_rate
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000
