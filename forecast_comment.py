"""A short comment to post on Metaculus, summarised from the full rationale.

The forecasters' ``result.comment`` is every ensemble run's whole rationale
joined together -- often 10-20 KB per question. That stays the record (runs.md);
this module only produces the text that is POSTED, so the bot does not flood
the site.

Cosmetic only: it runs after the forecast is final, reads nothing back into
it, and can never fail a question. Free SoCLaaS Qwen only; when Qwen is
unavailable or misbehaves, just the final-forecast line is posted.
"""
from __future__ import annotations

import logging
import re

import llm_provider
from llm_client import call_llm

logger = logging.getLogger(__name__)

LABEL = "forecast-comment"
MAX_POSTED_CHARS = 1200
_MAX_INPUT_CHARS = 60_000

_PROMPT = """You are writing the public comment a forecasting bot posts next to its forecast.

Question: {title}
Question type: {question_type}
Final forecast: {forecast}

Full rationale from the forecasting runs:
<<<
{rationale}
>>>

Write a short comment, at most 120 words, in this shape:
- One line stating the final forecast.
- 3 to 5 bullets with the key evidence and reasoning behind it.
- One line on the main uncertainty.

Use only what the rationale says: add no new facts, numbers or sources. Plain text,
no headings, no preamble. Output only the comment."""


def _qwen_available() -> bool:
    route = llm_provider.route_for_model(llm_provider.QWEN_RESEARCH_MODEL)
    return route.endpoint.label == "SoCLaaS" and bool(llm_provider.api_key_for(route.endpoint))


def _cap(text: str, limit: int = MAX_POSTED_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # End on a sentence or line boundary when one is reasonably close.
    boundary = max(cut.rfind(". "), cut.rfind("\n"))
    if boundary > limit * 0.6:
        cut = cut[: boundary + 1]
    return cut.rstrip() + " ..."


def fallback_comment(full_comment: str) -> str:
    """The comment's header line (the final forecast) and nothing else.

    An excerpt of the rationale was tried and read badly: the first run opens
    with its "Phase 0 -- research audit" scaffolding, not with its conclusion.
    """
    header = (full_comment or "").strip().partition("\n")[0]
    header = re.sub(r"[`*]", "", header).strip()
    return _cap(f"{header}\n(Bot forecast; detailed reasoning summary unavailable for this run.)")


async def short_comment(*, title: str, question_type: str, forecast: object,
                        full_comment: str) -> str:
    """Return the text to post. Never raises."""
    if not (full_comment or "").strip():
        return ""
    try:
        if not _qwen_available():
            logger.info("[forecast-comment] SoCLaaS Qwen not configured; posting excerpt")
            return fallback_comment(full_comment)
        rationale = full_comment[:_MAX_INPUT_CHARS]
        answer = await call_llm(
            _PROMPT.format(title=title, question_type=question_type or "unknown",
                           forecast=str(forecast)[:400], rationale=rationale),
            model=llm_provider.QWEN_RESEARCH_MODEL,
            temperature=0.3,
            _label=LABEL,
            max_tokens=1200,
        )
        answer = (answer or "").strip()
        if len(answer) < 40:  # empty or a refusal-sized stub
            raise ValueError(f"summary too short ({len(answer)} chars)")
        return _cap(answer)
    except Exception as exc:  # noqa: BLE001 - cosmetic step, must never fail a question
        logger.warning("[forecast-comment] summary failed (%s); posting excerpt", exc)
        return fallback_comment(full_comment)
