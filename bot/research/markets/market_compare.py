"""How each kept prediction market compares to the forecasting question.

One LLM call per platform, after relevance scoring, for the (at most three)
markets kept. For each market the model lists, in its own words:

  only_market    outcomes that would resolve the market Yes but not the question
  only_question  outcomes that would resolve the question Yes but not the market
  both           what they share, in one line

and tags every difference small or large. The verdict is derived here in code,
never by the model, so loose wording cannot turn a lower limit into an upper
one:

  nothing either side          -> Same question
  only the market counts more  -> Upper limit  (the question's chance <= price)
  only the question counts more-> Lower limit  (the question's chance >= price)
  both, all small              -> Close estimate (a starting point, adjusted)
  both, any large              -> Related only (context, no limit)

Why here (2026-11-06, Q46548): the brief compiler called a Polymarket price a
ceiling after checking one direction only -- the market counted only senior
officials, the question any negotiator -- and a forecaster trimmed below it.
The comparison is made once, next to each market's full resolution text,
instead of being re-derived from truncated text by the compiler and every
forecaster.
"""
from __future__ import annotations

import datetime
import json
import logging
import re

from bot.llm import clients as llm_clients

logger = logging.getLogger(__name__)

# Routed by label: "<platform>/market-comparison" falls under the platform's
# research prefix, so it runs where that platform's relevance scoring runs.
COMPARISON_MODEL = "anthropic/claude-sonnet-5"
MAX_MARKET_TEXT_CHARS = 3_000
MAX_QUESTION_CHARS = 4_000

SAME, UPPER, LOWER, CLOSE, RELATED = (
    "Same question", "Upper limit", "Lower limit", "Close estimate", "Related only")
_GUIDANCE = {
    SAME: "use the price directly",
    UPPER: "this question's chance is at most the market price",
    LOWER: "this question's chance is at least the market price",
    CLOSE: "a good starting point; adjust for the differences listed",
    RELATED: "context only; no limit",
}


def verdict(only_market: list[dict], only_question: list[dict]) -> str:
    """The comparison label, from which lists are empty and how big the
    differences are. An unparseable size counts as large (the cautious side)."""
    if not only_market and not only_question:
        return SAME
    if only_market and not only_question:
        return UPPER
    if only_question and not only_market:
        return LOWER
    sizes = {str(d.get("size", "")).strip().lower() for d in (*only_market, *only_question)}
    return CLOSE if sizes <= {"small"} else RELATED


def _clean_items(value) -> list[dict]:
    items = []
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, dict) and str(entry.get("text", "")).strip():
            items.append({"text": str(entry["text"]).strip(), "size": str(entry.get("size", "")).strip().lower()})
        elif isinstance(entry, str) and entry.strip() and entry.strip().lower() not in {"nothing", "none"}:
            items.append({"text": entry.strip(), "size": ""})
    return items


def _prompt(question: str, markets: list[str]) -> str:
    today = datetime.date.today().isoformat()
    numbered = "\n\n".join(f"MARKET {i + 1}:\n{text[:MAX_MARKET_TEXT_CHARS]}" for i, text in enumerate(markets))
    return f"""Compare each prediction market below with this forecasting question.

FORECASTING QUESTION (title, resolution criteria, fine print):
{question[:MAX_QUESTION_CHARS]}

{numbered}

For each market, read its resolution text against the question's criteria and fine print
and list, in plain words specific to this case:
- only_market: outcomes that would resolve the MARKET Yes but NOT the question (e.g. a
  broader place or format the market accepts, a longer deadline, a looser threshold).
- only_question: outcomes that would resolve the QUESTION Yes but NOT the market (e.g. people
  or formats the question accepts that the market excludes, an earlier or different window).
- both: one line on what they share.
Check every part of both rules: who or what counts, threshold or level, place and format,
time window and deadline, and the source that decides. Today is {today}. These markets are
still open, so nothing that would have resolved them has happened yet: leave out differences
about time already past, and compare only what can still happen from today. Tag each
difference by how often it would realistically decide the outcome in this case, not by
whether it is possible in principle: "small" (rarely) or "large" (often). An empty list
means nothing.
For a market with several outcomes, compare the outcome or sub-market closest to the
question and name it in "both". Do not estimate probabilities.

Return only JSON, one object per market in order:
[{{"market": 1, "only_market": [{{"text": "...", "size": "small"}}],
   "only_question": [{{"text": "...", "size": "large"}}], "both": "..."}}]"""


def compare_markets(question: str, markets: list[str], label: str) -> list[dict | None]:
    """One comparison per market text (same order), or None for any market the
    call could not cover. Never raises: a failed comparison only leaves the
    markets section as it was."""
    if not markets or not question.strip():
        return [None] * len(markets)
    try:
        response = llm_clients.sync_chat(
            label, COMPARISON_MODEL,
            {"messages": [{"role": "user", "content": _prompt(question, markets)}], "temperature": 0})
        content = (response.choices[0].message.content or "").strip()
        match = re.search(r"\[.*\]", content, re.S)
        rows = json.loads(match.group()) if match else []
    except Exception as exc:  # noqa: BLE001 - comparisons are an addition, never a blocker
        logger.warning("[%s] market comparison failed: %s: %s", label, type(exc).__name__, exc)
        return [None] * len(markets)
    out: list[dict | None] = [None] * len(markets)
    for position, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        index = row.get("market")
        index = int(index) - 1 if isinstance(index, (int, float)) or str(index).isdigit() else position
        if not 0 <= index < len(markets):
            continue
        only_market, only_question = _clean_items(row.get("only_market")), _clean_items(row.get("only_question"))
        out[index] = {"verdict": verdict(only_market, only_question), "only_market": only_market,
                      "only_question": only_question, "both": str(row.get("both", "")).strip()}
    return out


def format_comparison(comparison: dict | None, indent: str = "    ") -> list[str]:
    """The comparison as lines for a market's entry in the markets section."""
    if not comparison:
        return [f"{indent}How this market compares to the question: comparison unavailable"]

    def items(entries: list[dict]) -> str:
        if not entries:
            return "nothing"
        return "; ".join(f"{e['text']} ({e['size'] or 'size not stated'})" for e in entries)

    v = comparison["verdict"]
    lines = [
        f"{indent}How this market compares to the question -> {v} ({_GUIDANCE[v]})",
        f"{indent}  Only the market counts: {items(comparison['only_market'])}",
        f"{indent}  Only this question counts: {items(comparison['only_question'])}",
    ]
    if comparison.get("both"):
        lines.append(f"{indent}  Both: {comparison['both']}")
    return lines
