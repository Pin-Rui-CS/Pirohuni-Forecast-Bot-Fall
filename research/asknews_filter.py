"""Filter the raw AskNews block down to what bears on the question.

AskNews returns 6 "latest news" + 10 "news knowledge" articles for every
question, formatted by ``AskNewsSearcher._format_articles``. It is the second
largest section of the compiler input in both audited runs (21,302 chars =
18.6% of 44879's, 23,820 = 21.6% of 44880's) and it earns that space very
unevenly: 44879's produced the discount that stopped the AI-surge narrative
moving the forecast, while 44880's was campaign-logistics coverage that no
forecaster cited once.

This is a FILTER, not a summariser. Articles that survive keep their exact
wording and their full citation block, so:

  * ``compiler._parse_articles`` still matches (the ``**title**`` / body /
    ``Publish date:`` / ``Source:[id](url)`` shape is the contract), which
    keeps the compiler's deterministic duplicate-article collapse working;
  * ``source_ledger.record_text_urls`` still finds every surviving URL;
  * nothing downstream needs to know the filter ran.

Running it before the evidence plan is the point: the plan used to read
``asknews_research[:12_000]`` — 56% of the block, chosen by position rather
than relevance — and the query generator read a 14,000-char head cut of
AskNews-then-plan that consumed its whole budget before the plan started.
"""

from __future__ import annotations

import logging

from llm_client import call_llm
from monetary_cost_manager import HardLimitExceededError
import research_trace

logger = logging.getLogger(__name__)

DEFAULT_ASKNEWS_FILTER_MODEL = "anthropic/claude-sonnet-5"
# Below this the block is already small enough that a filter call costs more
# than it saves.
_MIN_CHARS_TO_FILTER = 6_000


async def filter_asknews_research(
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    asknews_research: str = "",
    model: str = DEFAULT_ASKNEWS_FILTER_MODEL,
) -> str:
    """Return the AskNews block with non-bearing material removed.

    Every failure is soft: the raw block is returned unchanged, so this can
    only ever cost tokens, never evidence.
    """
    raw = str(asknews_research or "").strip()
    if len(raw) < _MIN_CHARS_TO_FILTER:
        return raw

    prompt = _build_prompt(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        asknews_research=raw,
    )
    try:
        filtered = await call_llm(
            prompt,
            model=model,
            temperature=0.1,
            use_tools=False,
            _label="asknews-filter",
        )
    except HardLimitExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - filtering is best-effort
        logger.warning(
            "AskNews filter failed (%s: %s); using the unfiltered block.",
            type(exc).__name__,
            exc,
        )
        research_trace.emit(
            "asknews_filter",
            "AskNews filter",
            "",
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            meta={"model": model, "input_chars": len(raw)},
        )
        return raw

    filtered = str(filtered or "").strip()
    if not _is_usable(filtered, raw):
        logger.warning(
            "AskNews filter returned an unusable block (%d chars from %d); "
            "using the unfiltered block.",
            len(filtered),
            len(raw),
        )
        research_trace.emit(
            "asknews_filter",
            "AskNews filter",
            filtered,
            status="rejected",
            error="filtered block was empty or lost the article citation format",
            meta={"model": model, "input_chars": len(raw)},
        )
        return raw

    research_trace.emit(
        "asknews_filter",
        "AskNews filter",
        filtered,
        meta={
            "model": model,
            "input_chars": len(raw),
            "output_chars": len(filtered),
        },
    )
    logger.info(
        "[research] AskNews filter: %d -> %d chars", len(raw), len(filtered)
    )
    return filtered


def _is_usable(filtered: str, raw: str) -> bool:
    """Reject a reply that dropped the citation format or kept nothing.

    The format check is what protects the compiler's article parser: a reply
    that paraphrased the block into prose would silently disable
    ``_parse_articles`` and the duplicate collapse behind it.
    """
    if not filtered:
        return False
    if "Source:[" not in filtered:
        return False
    # An "everything is irrelevant" verdict is a filter failure, not a result:
    # the forecaster is better served by unfiltered news than by nothing.
    return "Publish date:" in filtered


def _build_prompt(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    asknews_research: str,
) -> str:
    return f"""
You are filtering a block of news articles gathered for a forecasting question,
before the rest of the research pipeline reads it.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or "Not provided."}

Background:
{background or "Not provided."}

Fine print:
{fine_print or "Not provided."}

Your job is to REMOVE what does not bear on this question and RETURN THE REST
UNCHANGED. You are a filter, not a summariser.

What to keep:
- Any article carrying a concrete fact that bears on the question: figures,
  counts, dates, rules, thresholds, named actors, scheduled events, or a stated
  trend. Keep it even if it cuts against the apparent majority reading of the
  block — both directions must survive.
- Anything touching the resolution source itself: how it is published, how
  often it updates, and what it has reported.
- If you are unsure whether an article bears on the question, KEEP it. The cost
  of keeping a marginal article is a few hundred characters; the cost of
  dropping a load-bearing one is a forecast built without it.

What to remove:
- Whole articles that carry no fact bearing on the question: unrelated topics,
  pure commentary, and process coverage with no dated content.
- A repeat of an article you have already kept: keep the fuller copy and delete
  the duplicate outright.
- Inside an article you keep, sentences that are pure boilerplate, syndication
  notices, or subscription/navigation text.

Rules you must not break:
- NEVER rewrite, paraphrase, summarise, translate, reorder or "clean up" the
  text of an article you keep. Reproduce its wording character for character.
  You may only delete whole articles and whole sentences.
- Reproduce each kept article in its original format, with its heading line and
  all of its trailing metadata lines intact and unaltered:
  **Title**, then the body, then "Original language:", "Publish date:", and
  "Source:[name](url)". Every kept article MUST keep its Publish date and
  Source lines — they are how the rest of the pipeline dates and cites it.
- Keep the articles in the order they appear below.
- Do not add commentary, headings, counts, or notes of your own. Do not say
  what you removed. Return only the surviving article blocks.
- Preserve the first line of the block ("Here are the relevant news articles:")
  exactly as it appears.

Articles to filter:
{asknews_research}
""".strip()
