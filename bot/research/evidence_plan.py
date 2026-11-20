from __future__ import annotations

import json

from bot.llm.client import call_llm
from bot.llm.cost_manager import HardLimitExceededError
from bot.utils import _truncate_text
from bot.trace import research_trace


DEFAULT_EVIDENCE_PLAN_MODEL = "anthropic/claude-sonnet-5"
# Safety net, not a routine cut. At 12,000 this discarded 44% of the AskNews
# block by position — the plan named the required artifact from the surviving
# head, but which 56% it saw was decided by article order. AskNews is now
# filtered for relevance before this call, so the whole filtered block fits.
_MAX_ASKNEWS_CHARS = 40_000

# Section headings that callers pick out of the formatted plan by name
# (plan_sections). Both _format_plan and _fallback_plan use these constants,
# so a renamed heading cannot silently stop matching.
ARTIFACT_HEADING = "Required Evidence Artifact"
# The forecast-like signal the forecast will most likely lean on (a poll,
# guidance, consensus, a stated plan, a betting favourite) and the record that
# shows how far to trust it. Added 2026-11-15: in the worst-scoring resolved
# questions the missing piece was often that record (Q46724's poll vs past
# results, NYT's guidance vs actuals), which no step asked for.
MAIN_SIGNAL_HEADING = "Main Signal And Its Track Record"
# The tables the history builder starts from (added 2026-11-19): the plan sees
# the whole question, so it chooses them; the builder may change them.
HISTORY_TABLES_HEADING = "History Tables To Build"
DIRECT_EVIDENCE_HEADING = "Direct Evidence To Find"
NEAR_PROXY_HEADING = "Near Proxy Evidence To Find"
# What the prediction-market searches read: enough to recognise a market that
# measures the question or a close proxy of it, without the rest of the plan.
MARKET_SEARCH_SECTIONS = (ARTIFACT_HEADING, DIRECT_EVIDENCE_HEADING, NEAR_PROXY_HEADING)


class EvidencePlan(str):
    """The plan's Markdown text, as every step reads it, carrying the history
    tables it set up as data (``history`` = {"tables": [...], "queries": [...]},
    empty when the plan gave none). Only the history builder reads the data; to
    everything else this is the same string as before."""

    history: dict

    @classmethod
    def of(cls, text: str, history: dict | None = None) -> "EvidencePlan":
        plan = cls(text)
        plan.history = history or {"tables": [], "queries": []}
        return plan


def plan_history(plan) -> dict:
    """The history tables and queries an evidence plan set up, or empty ones."""
    return getattr(plan, "history", None) or {"tables": [], "queries": []}


def _parse_history(parsed: dict) -> dict:
    """The plan's history_tables / history_queries, kept within the history
    builder's limits (2 tables, 6 columns, 3 queries); malformed tables dropped."""
    tables = []
    for spec in parsed.get("history_tables") if isinstance(parsed.get("history_tables"), list) else []:
        if not isinstance(spec, dict):
            continue
        columns = [str(c).strip() for c in spec.get("columns") or [] if str(c).strip()][:6]
        if len(columns) < 2:
            continue
        tables.append({"id": str(spec.get("id") or "AB"[len(tables)]).strip().upper()[:1] or "AB"[len(tables)],
                       "title": str(spec.get("title") or "").strip(), "row_is": str(spec.get("row_is") or "").strip(),
                       "columns": columns, "why": str(spec.get("why") or "").strip()})
        if len(tables) == 2:
            break
    queries = [str(q).strip() for q in parsed.get("history_queries") or [] if str(q).strip()][:3]
    return {"tables": tables, "queries": queries if tables else []}


async def build_evidence_plan(
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    asknews_research: str = "",
    model: str = DEFAULT_EVIDENCE_PLAN_MODEL,
) -> EvidencePlan:
    """Decide what evidence the rest of research should look for.

    This intentionally returns plain Markdown. The later search and compiler
    stages already consume text well, and keeping this human-readable makes the
    run logs easier to audit. The history tables it sets up also ride along as
    data (``EvidencePlan.history``) for the history builder's first move.
    """
    prompt = _build_prompt(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        asknews_research=asknews_research,
    )
    try:
        response = await call_llm(
            prompt,
            model=model,
            temperature=0.1,
            _label="evidence-plan",
        )
        parsed = _extract_json_object(response)
        plan = EvidencePlan.of(_format_plan(parsed), _parse_history(parsed))
        research_trace.emit("evidence_plan", "evidence plan", plan, meta={"model": model},
                            **_plan_lineage(asknews_research))
        return plan
    except HardLimitExceededError:
        raise
    except Exception as exc:
        plan = EvidencePlan.of(_fallback_plan(title, exc))
        research_trace.emit(
            "evidence_plan",
            "evidence plan",
            plan,
            status="fallback",
            error=f"{type(exc).__name__}: {exc}",
            meta={"model": model},
            **_plan_lineage(asknews_research),
        )
        return plan


def _plan_lineage(asknews_research: str) -> dict:
    """The plan reads the question (and the filtered AskNews block when given);
    it is both a document every step can read and the brief writer's first section."""
    reads = ["input:question"] + ([research_trace.ref_doc("asknews_filtered")] if asknews_research else [])
    return {"reads": reads, "writes": [research_trace.ref_doc("plan"), research_trace.ref_section("Evidence Plan")],
            "kind": "model", "lane": "*"}


def _build_prompt(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    asknews_research: str,
) -> str:
    return f"""
You are planning research for a forecasting bot.

The bot will search and scrape after this step. Your job is to decide what
information matters most, especially the required base-rate artifact.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or "Not provided."}

Background:
{background or "Not provided."}

Fine print:
{fine_print or "Not provided."}

AskNews research already gathered:
{_truncate_text(asknews_research or "No AskNews research was available.", _MAX_ASKNEWS_CHARS)}

Return only valid JSON in this shape:
{{
  "required_artifact": {{
    "name": "The one evidence artifact most needed before forecasting: a record that EXISTS TODAY (past values, past cases, a series), never the result the question is waiting for",
    "why_it_matters": "Why this is the base-rate anchor",
    "ideal_sources": ["official source", "primary dataset"],
    "fields_to_extract": ["year", "value", "source_url", "notes"]
  }},
  "main_signal": {{
    "signal": "The forward-looking signal a forecaster will most likely lean on (a poll, company guidance, an analyst consensus, an official forecast, a party's stated plan or date, a betting favourite), or 'none'",
    "track_record_needed": "The record that shows how far to trust that KIND of signal: its past values next to what actually happened (past polls vs results, past guidance vs reported figures, past stated dates vs when things happened), or empty when signal is 'none'",
    "track_record_queries": ["search query for that record"]
  }},
  "resolution_mechanics": {{
    "source_update_behavior": "Does the resolution source update or publish again before the resolution deadline? State the known or likely cadence, the next expected update, and what evidence would pin it down",
    "update_content_limits": "What new information CAN appear in the source before the deadline, and what CANNOT arrive in time (reporting calendars, disclosure deadlines, data-pipeline or publication lags); and whether the same measurement can already be read earlier elsewhere (a daily or preliminary series, or a tracker from the same publisher or its data provider): name it and the period it covers by now",
    "mechanics_queries": ["search query targeting the source's update schedule or the underlying reporting calendar"]
  }},
  "history_tables": [
    {{"id": "A", "title": "...", "row_is": "one ... (what one row is)", "columns": ["date or case name", "..."], "why": "what a forecaster can count from it"}},
    {{"id": "B", "title": "...", "row_is": "...", "columns": ["..."], "why": "..."}}
  ],
  "history_queries": ["search query for a page that HOLDS such a table"],
  "direct_evidence": ["Evidence that directly resolves or measures the question"],
  "near_proxy_evidence": ["Evidence that is close but not exact"],
  "weak_proxy_evidence": ["Evidence that is only indirectly related"],
  "background_color": ["Context that may be interesting but should not move the forecast much"],
  "contradictions_to_check": ["Conflicting claims or missing facts to audit"],
  "search_queries": ["concise search query", "another concise search query"]
}}

Rules:
- The required artifact is something a forecaster can use BEFORE the outcome is known: how often or how fast this kind of thing has happened (past values of the quantity, past cases of the event). Never name the resolution result itself (the election's official results, the filing the question waits for): it cannot exist yet. The resolution source belongs under resolution_mechanics.
- history_tables are the tables a later research step will fill with PAST values or PAST cases copied from pages, for forecasters to count from (at most 2 tables, at most 6 columns each; the first column identifies the row: a date YYYY, YYYY-MM or YYYY-MM-DD, or the case's name). Table A is the history the question is about (past values of its quantity, or past cases of its event). Table B is whichever helps a forecaster more: a wider comparison group that says how often this kind of thing happens across similar cases (e.g. for one company's next move, how often the same company made that move across all its holdings or past years), or the track record of the main signal (its past values next to what happened). Give at most 3 history_queries, for pages that hold such tables.
- Prefer official, source-of-truth artifacts over broad news.
- For count questions, the required artifact is usually a historical count table.
- For direct prediction markets, distinguish exact-human/outcome markets from adjacent proxy markets.
- When the question resolves off a curated page, tracker, leaderboard, or scheduled data release ("as shown/displayed on X as of date D"), the source's update mechanics — whether it refreshes before the deadline and what a refresh can legally or physically contain — are often MORE decision-relevant than the underlying race. Fill resolution_mechanics with concrete cadences, calendars, and lags to verify (e.g. filing deadlines, release schedules, disclosure windows), and add mechanics_queries that target them specifically. If the official figure only comes out after the period ends, look for those earlier readings; never write "no data before the release" without checking. List a partial reading of the resolution period itself under direct_evidence, a reading of the preceding period under near_proxy_evidence, and add a query for their latest values to mechanics_queries.
- When the question resolves by direct observation of an event rather than a published source, set both resolution_mechanics text fields to "Not applicable — resolves by direct observation of the event" and leave mechanics_queries empty.
- Do not forecast or estimate probabilities.
""".strip()


def _extract_json_object(text: str) -> dict:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Could not extract evidence-plan JSON from response: {text[:500]}")


def _format_plan(plan: dict) -> str:
    artifact = plan.get("required_artifact") if isinstance(plan.get("required_artifact"), dict) else {}
    mechanics = plan.get("resolution_mechanics") if isinstance(plan.get("resolution_mechanics"), dict) else {}
    signal = plan.get("main_signal") if isinstance(plan.get("main_signal"), dict) else {}
    search_queries = plan.get("search_queries") if isinstance(plan.get("search_queries"), list) else []
    mechanics_queries = mechanics.get("mechanics_queries") if isinstance(mechanics.get("mechanics_queries"), list) else []
    signal_queries = signal.get("track_record_queries") if isinstance(signal.get("track_record_queries"), list) else []
    combined_queries = list(search_queries)
    for query in mechanics_queries + signal_queries:
        if query not in combined_queries:
            combined_queries.append(query)
    signal_name = str(signal.get("signal") or "").strip()
    no_signal = not signal_name or signal_name.lower() in {"none", "n/a", "not applicable"}
    lines = [
        "# Evidence Plan",
        "",
        f"## {ARTIFACT_HEADING}",
        f"- Name: {artifact.get('name') or 'Not identified.'}",
        f"- Why it matters: {artifact.get('why_it_matters') or 'Not stated.'}",
        f"- Ideal sources: {_format_list(artifact.get('ideal_sources'))}",
        f"- Fields to extract: {_format_list(artifact.get('fields_to_extract'))}",
        "",
        f"## {MAIN_SIGNAL_HEADING}",
        f"- Signal: {'None identified.' if no_signal else signal_name}",
        f"- Track record to find: {'Not applicable.' if no_signal else (str(signal.get('track_record_needed') or '').strip() or 'Not stated.')}",
        "",
        f"## {HISTORY_TABLES_HEADING}",
        *_history_lines(_parse_history(plan)),
        "",
        "## Resolution Mechanics",
        f"- Source update behavior: {mechanics.get('source_update_behavior') or 'Not assessed.'}",
        f"- What an update can contain before the deadline: {mechanics.get('update_content_limits') or 'Not assessed.'}",
        "",
        f"## {DIRECT_EVIDENCE_HEADING}",
        *_format_bullets(plan.get("direct_evidence")),
        "",
        f"## {NEAR_PROXY_HEADING}",
        *_format_bullets(plan.get("near_proxy_evidence")),
        "",
        "## Weak Proxy Evidence To Treat Cautiously",
        *_format_bullets(plan.get("weak_proxy_evidence")),
        "",
        "## Background Color",
        *_format_bullets(plan.get("background_color")),
        "",
        "## Contradictions And Gaps To Check",
        *_format_bullets(plan.get("contradictions_to_check")),
        "",
        "## Search Queries To Prefer",
        *_format_bullets(combined_queries),
    ]
    return "\n".join(lines).strip()


def _fallback_plan(title: str, exc: Exception) -> str:
    return f"""
# Evidence Plan

Evidence planning failed: {type(exc).__name__}: {exc}

## {ARTIFACT_HEADING}
- Name: Historical or official source-of-truth evidence for: {title}
- Why it matters: The forecast should be anchored to the most direct base-rate or official artifact available.
- Ideal sources: official source, primary dataset, reputable archive
- Fields to extract: date or year, value or status, source_url, notes

## {MAIN_SIGNAL_HEADING}
- Signal: Not assessed (evidence planning failed).
- Track record to find: Not assessed (evidence planning failed).

## {HISTORY_TABLES_HEADING}
- None set up (evidence planning failed).

## Resolution Mechanics
- Source update behavior: Not assessed (evidence planning failed).
- What an update can contain before the deadline: Not assessed (evidence planning failed).

## {DIRECT_EVIDENCE_HEADING}
- Official resolution-source facts and direct markets that match the exact question.

## {NEAR_PROXY_HEADING}
- Closely related historical or current evidence for the same entities and outcome type.

## Weak Proxy Evidence To Treat Cautiously
- Adjacent markets, commentary, or technology/context signals that do not directly measure the outcome.

## Background Color
- General news that helps understand the setting but should not move the forecast much.

## Contradictions And Gaps To Check
- Missing historical rows, stale data, and conflicting claims.
""".strip()


def _history_lines(history: dict) -> list[str]:
    if not history["tables"]:
        return ["- None set up."]
    lines = []
    for t in history["tables"]:
        lines.append(f"- Table {t['id']}: {t['title'] or '(untitled)'} -- one row = {t['row_is'] or '(not stated)'}; "
                     f"columns: {', '.join(t['columns'])}" + (f". Why: {t['why']}" if t["why"] else ""))
    if history["queries"]:
        lines.append("- Searches: " + "; ".join(history["queries"]))
    return lines


def _format_list(value) -> str:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(cleaned) if cleaned else "Not stated."
    text = str(value or "").strip()
    return text or "Not stated."


def _format_bullets(value) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    bullets = [f"- {str(item).strip()}" for item in value if str(item).strip()]
    return bullets or ["- None stated."]


def plan_sections(plan: str, headings: tuple[str, ...]) -> str:
    """The named "## " sections of a formatted plan, whole and in plan order.

    Returns "" when none of the headings is present (e.g. a plan that is not in
    the _format_plan shape); callers decide what to send instead.
    """
    kept = []
    # [0] is the "# Evidence Plan" title (and, for a fallback plan, its error line).
    for block in str(plan or "").split("\n## ")[1:]:
        if block.split("\n", 1)[0].strip() in headings:
            kept.append("## " + block.strip())
    return "\n\n".join(kept)


