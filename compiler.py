from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable

import llm_provider
from config import llm_rate_limiter
from monetary_cost_manager import (
    HardLimitExceededError,
    MonetaryCostManager,
)
from utils import _truncate_text
import research_trace

logger = logging.getLogger(__name__)

ProviderResult = tuple[str, str]

_DEFAULT_MODEL = "anthropic/claude-opus-5"
# Market provider sections only (already line-filtered; small). Research
# sections are NOT hard-truncated any more — see _fit_sections_to_budget.
_MAX_PROVIDER_CHARS = 24_000
# Total budget for all sections handed to the compiler LLM. When the combined
# research exceeds it, oversized sections are COMPRESSED WITH A READER (a
# cheaper LLM pass that must preserve every distinct claim), never cut with
# [:N]. A head-keep [:N] here is what caused the 44619 miss: the 24K
# per-provider cut silently discarded all of the YES-leaning evidence, which
# sat deep in the search-snippet dump, and the compiler built a one-sided
# brief from what was left.
_COMPILER_INPUT_BUDGET_CHARS = 120_000
# Output cap for the brief. Was a hard-coded 6000, which 44875 hit exactly —
# the brief died mid-token and the failure was invisible because the only
# check was `if not content.strip()`. Output is the minority of this call's
# cost (41% on the 44875 run), so headroom here is cheap next to shipping a
# brief whose Market Signals section an evidence item references but which
# does not exist.
_COMPILER_MAX_OUTPUT_TOKENS = 10_000
_TRUNCATION_NOTICE = (
    "\n\n---\n**[COMPILER TRUNCATION WARNING]** This brief hit the compiler's output "
    "limit and stops mid-sentence above. Sections that would have followed are MISSING, "
    "not empty — if an evidence item references a section you cannot find, that is why. "
    "Treat the absence of a section as unknown, never as evidence of nothing.\n"
)
_PRECOMPRESS_MODEL = "anthropic/claude-sonnet-5"
_MAX_PRECOMPRESS_CALLS = 3
_PRECOMPRESS_CHUNK_CHARS = 90_000
_PRECOMPRESS_MIN_TARGET_CHARS = 10_000
_MAX_ARTICLE_BODY_CHARS = 2_400
_MAX_KEY_EVIDENCE_ITEMS = 10
_SIMILAR_ARTICLE_THRESHOLD = 0.82

_ARTICLE_PATTERN = re.compile(
    r"\*\*(?P<title>[^\n*][^\n]*?)\*\*\s*\n"
    r"(?P<body>.*?)(?:\nOriginal language:\s*(?P<language>[^\n]*))?"
    r"\nPublish date:\s*(?P<publish_date>[^\n]*)"
    r"\nSource:\[(?P<source>[^\]]*)\]\((?P<url>[^)]*)\)",
    re.DOTALL,
)
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_SEPARATOR_LINE = re.compile(r"^\s*[=\-]{5,}\s*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")
_DATE_HINT = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",
    re.IGNORECASE,
)
_NUMBER_HINT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|cases?|deaths?|days?|weeks?|months?|years?|M|K|million|billion)?\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://\S+")

_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "also",
    "among",
    "before",
    "being",
    "below",
    "between",
    "could",
    "does",
    "during",
    "from",
    "have",
    "into",
    "more",
    "most",
    "only",
    "other",
    "over",
    "public",
    "question",
    "resolve",
    "resolution",
    "should",
    "than",
    "that",
    "their",
    "there",
    "these",
    "this",
    "through",
    "under",
    "until",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
}


@dataclass(frozen=True)
class Article:
    title: str
    body: str
    publish_date: str
    source: str
    url: str
    language: str = ""

    @property
    def key(self) -> str:
        if self.url:
            return self.url.lower().strip()
        return _normalise_for_dedupe(f"{self.title} {self.source}")


@dataclass
class ArticleGroup:
    representative: Article
    articles: list[Article]


async def compile_research_report(
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    provider_results: Iterable[ProviderResult] | None = None,
    raw_research: str | None = None,
    model: str = _DEFAULT_MODEL,
    artifact_check: dict | None = None,
) -> str:
    """Compile raw research provider output into a forecast-ready brief.

    The compiler is selective: it distills the raw provider output into a
    ranked evidence table of decision-relevant items (each with exact values,
    dates, source, and URL), collapses syndicated duplicates into one item,
    and drops background color. If the LLM pass fails, it falls back to a
    deterministic cleaned brief rather than blocking the forecast.
    """
    cleaned_sections = _prepare_sections(provider_results, raw_research)
    if not cleaned_sections:
        return "No external research material found."

    heuristic_report = _build_heuristic_report(
        title=title,
        resolution_criteria=resolution_criteria,
        sections=cleaned_sections,
        artifact_check=artifact_check,
    )

    try:
        llm_report = await _try_llm_compile(
            title=title,
            resolution_criteria=resolution_criteria,
            background=background,
            fine_print=fine_print,
            cleaned_sections=cleaned_sections,
            model=model,
            artifact_check=artifact_check,
        )
    except HardLimitExceededError as exc:
        # The compile LLM pass (including precompress) was refused by the
        # token budget. The deterministic fallback costs zero LLM tokens, so
        # failing the question here would only discard the research already
        # paid for (44773 incident, 2026-07-16).
        logger.warning(
            "Research compiler LLM pass refused by the token budget (%s); "
            "falling back to the deterministic brief.",
            exc,
        )
        llm_report = None
    return llm_report or heuristic_report


def _prepare_sections(
    provider_results: Iterable[ProviderResult] | None,
    raw_research: str | None,
) -> list[ProviderResult]:
    sections: list[ProviderResult] = []

    if provider_results is not None:
        for provider_name, content in provider_results:
            cleaned = _clean_provider_content(provider_name, content)
            if cleaned:
                sections.append((provider_name.strip() or "Research", cleaned))

    if raw_research and raw_research.strip():
        sections.append(("Raw Research", _clean_generic_text(raw_research)))

    return sections


def _clean_provider_content(provider_name: str, content: str | None) -> str:
    if not content or not str(content).strip():
        return ""

    text = _clean_generic_text(str(content))
    lowered_name = provider_name.lower()
    lowered_text = text.lower()

    if "asknews" in lowered_name or "here are the relevant news articles" in lowered_text:
        articles = _parse_articles(text)
        if articles:
            return _format_articles_for_compiler(articles)

    if "kalshi" in lowered_name or "manifold" in lowered_name or "polymarket" in lowered_name:
        return _clean_market_text(text)

    # No [:N] truncation here. Research sections go to the compiler whole;
    # _fit_sections_to_budget compresses (with an LLM) only when the combined
    # total exceeds the compiler input budget.
    return text


def _clean_generic_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "")
    text = _SEPARATOR_LINE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


def _clean_market_text(text: str) -> str:
    useful_lines: list[str] = []
    keep_prefixes = (
        "found ",
        "[",
        "relevance score",
        "type",
        "volume",
        "liquidity",
        "total volume",
        "total liquidity",
        "url",
        "outcomes",
        "active sub-markets",
        "- ",
        "odds",
        "ticker",
        "event ticker",
        "yes probability",
        "bid/ask",
        "last price",
        "24h volume",
        "open interest",
        "close time",
        "api url",
        "subtitle",
        "rules",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if useful_lines and useful_lines[-1] != "":
                useful_lines.append("")
            continue
        lowered = line.lower()
        if (
            "research" in lowered
            or "crowd-implied probabilities" in lowered
            or lowered.startswith(keep_prefixes)
        ):
            useful_lines.append(line)

    cleaned = "\n".join(useful_lines).strip()
    return _truncate_text(cleaned or text, _MAX_PROVIDER_CHARS)


def _parse_articles(text: str) -> list[Article]:
    seen: set[str] = set()
    articles: list[Article] = []
    for match in _ARTICLE_PATTERN.finditer(text):
        article = Article(
            title=_collapse_spaces(match.group("title")),
            body=_collapse_spaces(match.group("body")),
            publish_date=_collapse_spaces(match.group("publish_date")),
            source=_collapse_spaces(match.group("source")),
            url=_collapse_spaces(match.group("url")),
            language=_collapse_spaces(match.group("language") or ""),
        )
        if not article.title or article.key in seen:
            continue
        seen.add(article.key)
        articles.append(article)
    return articles


def _format_articles_for_compiler(articles: list[Article]) -> str:
    groups = _group_similar_articles(articles)
    lines = [
        "AskNews articles visited, grouped by repeated or near-repeated content.",
        "Article wording below is lightly cleaned for whitespace only; repeated bodies are shown once, and every unique article citation is retained.",
    ]
    for idx, group in enumerate(groups, 1):
        representative = group.representative
        body = _truncate_text(representative.body, _MAX_ARTICLE_BODY_CHARS)
        lines.extend(
            [
                "",
                f"{idx}. Article content group: {representative.title}",
                "   Content:",
                f"   {body}",
                "   Articles visited for this content:",
            ]
        )
        for article in group.articles:
            lines.append(f"   - {_format_article_citation(article)}")
        if len(group.articles) > 1:
            lines.append(
                "   Note: These articles appear to share the same or substantially similar content; "
                "the content is shown once above to avoid repeated bodies."
            )
    return "\n".join(lines).strip()


def _group_similar_articles(articles: list[Article]) -> list[ArticleGroup]:
    groups: list[ArticleGroup] = []
    for article in articles:
        for group in groups:
            if _articles_share_content(article, group.representative):
                group.articles.append(article)
                break
        else:
            groups.append(ArticleGroup(representative=article, articles=[article]))
    return groups


def _articles_share_content(left: Article, right: Article) -> bool:
    if left.key == right.key:
        return True
    left_body = _normalise_for_dedupe(left.body)
    right_body = _normalise_for_dedupe(right.body)
    if left_body and left_body == right_body:
        return True
    if _normalise_for_dedupe(left.title) == _normalise_for_dedupe(right.title):
        return True

    left_tokens = _article_similarity_tokens(left)
    right_tokens = _article_similarity_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    containment = overlap / min(len(left_tokens), len(right_tokens))
    union_similarity = overlap / len(left_tokens | right_tokens)
    return containment >= _SIMILAR_ARTICLE_THRESHOLD or union_similarity >= 0.72


def _article_similarity_tokens(article: Article) -> set[str]:
    tokens = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", f"{article.title} {article.body}".lower()):
        if token not in _STOPWORDS:
            tokens.add(token)
    return tokens


def _format_article_citation(article: Article) -> str:
    parts = [article.title]
    if article.publish_date:
        parts.append(f"published {article.publish_date}")
    if article.source:
        parts.append(f"source {article.source}")
    if article.language:
        parts.append(f"language {article.language}")
    if article.url:
        parts.append(article.url)
    return " | ".join(parts)


_PRECOMPRESS_PROMPT = """
You are condensing one section of raw research so a downstream evidence compiler
can read all of it within its input budget. You are a lossless-as-possible
compressor, NOT a summarizer.

Rules:
- Keep EVERY distinct factual claim, statistic, date, quoted statement, market
  price, source name, and URL. Exact values and dates must survive verbatim.
- Remove only: navigation/boilerplate text, repeated headers, and content that
  is an exact or near-exact duplicate of content earlier in this same section
  (syndicated reposts of one story collapse to one entry listing the duplicate
  sources).
- DIRECTIONAL BALANCE IS MANDATORY: never drop a claim because it cuts against
  the apparent majority narrative of the section. If claims point both toward
  and against the event in question, both sides must survive compression.
- Preserve the section's heading structure and the original order of items.
- Target length: about {target_chars} characters. Completeness beats the
  target — if honoring the target would force dropping a distinct claim, run
  longer instead and say nothing about it.

Section name: {name}

Section content:
{content}
""".strip()


async def review_qwen_precompression(
    name: str, content: str, target_chars: int, **experiment_options,
):
    """Replay the actual compiler prompt on SoCLaaS, without invoking the compiler.

    Does not call Sonnet/Opus, submit forecasts, or accept semantic equivalence.
    The returned artifacts can be compared with an existing saved baseline.
    """
    from qwen_precompression import PrecompressionPaused, generate_candidate

    prompt = _PRECOMPRESS_PROMPT.format(name=name, target_chars=target_chars, content=content)
    try:
        candidate = await generate_candidate(
            name=name, source=content, messages=[{"role": "user", "content": prompt}],
            **experiment_options,
        )
    except HardLimitExceededError as exc:
        # Avoid the outer compiler's deterministic-brief fallback for this
        # quality-first experiment; a budget refusal must pause the experiment.
        raise PrecompressionPaused("Qwen precompression refused by usage budget") from exc
    research_trace.emit(
        "precompress", name, candidate.answer or "", status="experimental",
        meta={"artifact_dir": str(candidate.artifact_dir), "effort": candidate.selected_effort,
              "semantic_review": "pending", "input_chars": len(content)},
    )
    return candidate


async def _compress_section_text(name: str, content: str, target_chars: int) -> str | None:
    """Compress one section using the selected opt-in policy.

    None keeps the original in the Qwen experiment; the legacy path permits
    visible truncation. Review mode raises after saving the candidate.
    """
    from llm_client import call_llm
    from qwen_precompression import PrecompressionPaused, mode

    qwen_mode = mode()
    if qwen_mode != "off":
        candidate = await review_qwen_precompression(name, content, target_chars)
        if qwen_mode == "review":
            raise PrecompressionPaused(
                f"Qwen precompression saved for semantic review: {candidate.artifact_dir}"
            )
        if candidate.answer is None:
            # Keep the original until the complete section budget is checked.
            # The experimental branch never substitutes a truncation or paid call.
            return None
        return (
            "[Experimental Qwen condensation; mechanical checks passed, "
            f"semantic fidelity is not certified.]\n{candidate.answer}"
        )

    max_tokens = max(2_000, min(16_000, target_chars // 3))
    try:
        compressed = await call_llm(
            _PRECOMPRESS_PROMPT.format(
                name=name,
                target_chars=target_chars,
                content=content,
            ),
            model=_PRECOMPRESS_MODEL,
            temperature=0.1,
            max_tokens=max_tokens,
            _label="compiler/precompress",
            # This prompt promises a LOSSLESS compression and the caller
            # trusts it. A pass cut off at max_tokens keeps the entries before
            # the cut and silently drops every one after, while still reading
            # as a complete section (44879: cut mid-URL at entry [5] of 25,
            # then labelled itself "all distinct claims retained"). Treat it as
            # a failure so the caller falls back to a VISIBLE truncation.
            raise_on_truncation=True,
        )
    except HardLimitExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - compression is best-effort
        logger.warning(
            "compiler precompress failed for section %r: %s: %s",
            name, type(exc).__name__, exc,
        )
        # The exception type is otherwise lost (a $0 ledger row with no error
        # text — the 44804 "0.0s precompress" mystery). Record it.
        research_trace.emit(
            "precompress",
            name,
            "",
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            meta={"target_chars": target_chars, "input_chars": len(content)},
        )
        return None
    compressed = (compressed or "").strip()
    if not compressed:
        research_trace.emit(
            "precompress",
            name,
            "",
            status="failed",
            error="compression call returned empty output",
            meta={"target_chars": target_chars, "input_chars": len(content)},
        )
        return None
    research_trace.emit(
        "precompress",
        name,
        compressed,
        meta={"target_chars": target_chars, "input_chars": len(content)},
    )
    return (
        f"[Section condensed from {len(content):,} to {len(compressed):,} chars; "
        f"preservation of all distinct claims has not been verified.]\n{compressed}"
    )


def _visible_truncate(name: str, content: str, target_chars: int) -> str:
    """Last-resort cut. Unlike the old silent [:N], it names what was lost."""
    if target_chars >= len(content):
        return content
    dropped = len(content) - target_chars
    marker = (
        f"\n\n[COMPILER INPUT BUDGET TRUNCATION — {dropped:,} chars dropped from "
        f"the tail of section '{name}'. Evidence beyond this point was NOT seen "
        f"by the compiler; treat this section as incomplete.]"
    )
    logger.warning(
        "compiler budget truncation: dropped %d chars from section %r", dropped, name
    )
    research_trace.emit(
        "precompress",
        name,
        content[max(0, target_chars):],
        status="truncated",
        error=f"visible truncation dropped {dropped} chars from the tail",
        meta={"target_chars": target_chars, "input_chars": len(content)},
    )
    return content[: max(0, target_chars)].rstrip() + marker


async def _fit_sections_to_budget(
    sections: list[ProviderResult],
    budget: int = _COMPILER_INPUT_BUDGET_CHARS,
) -> list[ProviderResult]:
    """Fit the combined sections into the compiler's input budget without any
    silent loss.

    Typical questions fit as-is and pay nothing. When over budget, the largest
    NON-resolution sections are compressed with an LLM (resolution-source
    material is authoritative and compressed only if nothing else is left),
    up to _MAX_PRECOMPRESS_CALLS calls. Only if the budget is still exceeded
    afterwards is anything cut — and then with a loud in-band marker, never
    silently.
    """
    from qwen_precompression import PrecompressionPaused, mode

    qwen_mode = mode()
    total = sum(len(content) for _, content in sections)
    if total <= budget:
        return sections

    fitted: list[list[str]] = [[name, content] for name, content in sections]

    def _total() -> int:
        return sum(len(content) for _, content in fitted)

    # Compress research sections first (largest first); resolution-source
    # sections only as a last resort.
    def _is_resolution(index: int) -> bool:
        return "resolution" in fitted[index][0].lower()

    candidates = sorted(
        (i for i in range(len(fitted)) if not _is_resolution(i)),
        key=lambda i: len(fitted[i][1]),
        reverse=True,
    ) + sorted(
        (i for i in range(len(fitted)) if _is_resolution(i) and qwen_mode == "off"),
        key=lambda i: len(fitted[i][1]),
        reverse=True,
    )

    # Qwen XHigh finished article-sized inputs (A1 screen, median 235 s) but
    # timed out on the 80-89k-character production chunks, so the Qwen modes
    # compress smaller chunks. They are free in money, hence the larger call
    # allowance, and run concurrently (the SoCLaaS rate gate still applies).
    if qwen_mode == "off":
        chunk_chars, calls_left = _PRECOMPRESS_CHUNK_CHARS, _MAX_PRECOMPRESS_CALLS
    else:
        chunk_chars = int(os.getenv("SOCLAAS_PRECOMPRESS_CHUNK_CHARS", "25000"))
        calls_left = int(os.getenv("SOCLAAS_PRECOMPRESS_MAX_CALLS", "12"))
    for index in candidates:
        if _total() <= budget or calls_left <= 0:
            break
        name, content = fitted[index]
        overflow = _total() - budget
        target = max(
            len(content) - overflow,
            len(content) // 4,
            _PRECOMPRESS_MIN_TARGET_CHARS,
        )
        if target >= len(content):
            continue
        # A section larger than one compression input is split into chunks;
        # each chunk is its own call against the call budget.
        chunks = [
            content[i: i + chunk_chars]
            for i in range(0, len(content), chunk_chars)
        ]
        per_chunk_target = max(_PRECOMPRESS_MIN_TARGET_CHARS // 2, target // len(chunks))
        new_parts: list[str | None] = [None] * len(chunks)
        jobs: dict[int, str] = {}
        for idx, chunk in enumerate(chunks):
            if calls_left <= 0 or len(chunk) <= per_chunk_target:
                new_parts[idx] = chunk  # kept raw; final marker pass may cut it
                continue
            calls_left -= 1
            jobs[idx] = name if len(chunks) == 1 else f"{name} (part {idx + 1}/{len(chunks)})"
        if qwen_mode == "off":
            results = [await _compress_section_text(label, chunks[idx], per_chunk_target)
                       for idx, label in jobs.items()]
        else:
            # The gateway answered a fifth simultaneous stream with 429 while
            # four others were streaming (45707 A/B run, 2026-09-21).
            limit = asyncio.Semaphore(int(os.getenv("SOCLAAS_PRECOMPRESS_CONCURRENCY", "3")))

            async def bounded(label: str, chunk: str) -> str | None:
                async with limit:
                    return await _compress_section_text(label, chunk, per_chunk_target)

            results = await asyncio.gather(*(
                bounded(label, chunks[idx]) for idx, label in jobs.items()
            ))
        for (idx, label), compressed in zip(jobs.items(), results):
            if compressed is None:
                new_parts[idx] = (chunks[idx] if qwen_mode != "off"
                                  else _visible_truncate(label, chunks[idx], per_chunk_target))
            else:
                new_parts[idx] = compressed
        fitted[index][1] = "\n\n".join(part for part in new_parts if part is not None)
        logger.info(
            "compiler precompress: section %r %d -> %d chars",
            name, len(content), len(fitted[index][1]),
        )

    if _total() > budget:
        if qwen_mode != "off" and os.getenv("SOCLAAS_PRECOMPRESS_OVERFLOW", "pause").strip().lower() != "truncate":
            raise PrecompressionPaused(
                "Experimental Qwen precompression did not fit the compiler budget; "
                "originals retained. No truncation or paid recovery was attempted."
            )
        if qwen_mode != "off":
            logger.warning(
                "Qwen precompression left the sections %d chars over budget; "
                "SOCLAAS_PRECOMPRESS_OVERFLOW=truncate, so the largest research "
                "section is cut VISIBLY instead of pausing.", _total() - budget,
            )
        # Still over after the compression budget: cut the largest research
        # section visibly. Resolution sections are never cut here.
        research_indexes = [i for i in range(len(fitted)) if not _is_resolution(i)]
        if research_indexes:
            largest = max(research_indexes, key=lambda i: len(fitted[i][1]))
            name, content = fitted[largest]
            target = max(_PRECOMPRESS_MIN_TARGET_CHARS, len(content) - (_total() - budget))
            if target < len(content):
                fitted[largest][1] = _visible_truncate(name, content, target)

    return [(name, content) for name, content in fitted]


async def _try_llm_compile(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    cleaned_sections: list[ProviderResult],
    model: str,
    artifact_check: dict | None = None,
) -> str | None:
    # The key that matters is the one for THIS model's endpoint, which under a
    # mixed routing profile is not necessarily "the" provider's.
    route = llm_provider.route_for(model, label="compiler/research-brief")
    if not llm_provider.api_key_for(route.endpoint):
        logger.info(
            "Research compiler skipped LLM pass because %s is not set.",
            route.endpoint.api_key_env,
        )
        return None

    model = route.model_id

    cleaned_sections = await _fit_sections_to_budget(cleaned_sections)
    # Byte-exact record of what the compiler can know. "Dropped by the
    # compiler" vs "never reached the compiler" (the 44619 forensic) becomes
    # a grep against this payload.
    research_trace.emit(
        "compiler_input",
        "fitted sections (byte-exact compiler input)",
        "\n\n".join(
            f"===== SECTION: {name} ({len(content):,} chars) =====\n{content}"
            for name, content in cleaned_sections
        ),
        meta={
            "model": model,
            "sections": [
                {"name": name, "chars": len(content)} for name, content in cleaned_sections
            ],
        },
    )

    prompt = _build_compiler_prompt(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        cleaned_sections=cleaned_sections,
        artifact_check=artifact_check,
    )
    if route.endpoint.name == llm_provider.ENDPOINT_SOCLAAS.name:
        prompt = apply_brief_variant(prompt, os.getenv("QWEN_BRIEF_PROMPT", "").strip() or "base")

    client = llm_provider.async_client_for(route)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a research compiler for a forecasting bot. You filter raw "
                "research into a detailed evidence showcase. You select only "
                "decision-relevant items, keep exact values, dates, source names, "
                "and URLs, collapse syndicated duplicates into one item, and never "
                "estimate probabilities or invent facts."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        await llm_provider.gate_for(route).wait_async()
        async with llm_rate_limiter:
            usage_handle = MonetaryCostManager.start_openrouter_call(
                "compiler/research-brief",
                model,
                {"messages": messages, "max_tokens": _COMPILER_MAX_OUTPUT_TOKENS},
            )
            response = await client.chat.completions.create(
                **llm_provider.build_kwargs(
                    route,
                    {
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": _COMPILER_MAX_OUTPUT_TOKENS,
                        "stream": False,
                    },
                )
            )
        usage_handle.record_response(response)
        logger.info(
            "research-compiler | model=%s | %s usage recorded",
            model,
            route.endpoint.label,
        )
        choice = response.choices[0]
        content = choice.message.content
        if not content or not content.strip():
            # 44880: 123,704 tokens in, 0 out, $0.00 — the brief was swapped
            # for the heuristic fallback with no log line anywhere, detectable
            # only from an all-zeros audit row. Unlike call_llm, this path
            # builds its request directly and so never reaches
            # _validate_text_completion_response. Say it out loud.
            logger.warning(
                "research-compiler | model=%s returned an EMPTY brief "
                "(finish_reason=%s); falling back to the deterministic brief.",
                model,
                getattr(choice, "finish_reason", "unknown"),
            )
            research_trace.emit(
                "brief",
                "compiler returned an empty brief",
                "",
                status="failed",
                error=f"empty content, finish_reason={getattr(choice, 'finish_reason', 'unknown')}",
                meta={"chain": "brief", "model": model},
            )
            return None
        # A brief cut off at the output cap is non-empty, so the old
        # `if not content.strip()` guard passed it through silently: 44875
        # shipped a brief ending mid-token at "- [I2] From [E", losing the rest
        # of Derived Implications and the whole Market Signals section that an
        # evidence item pointed at. finish_reason is the only signal that
        # distinguishes that from a brief the model chose to end.
        if str(getattr(choice, "finish_reason", "") or "").lower() == "length":
            logger.warning(
                "research-compiler | brief hit the %d-token output cap and is TRUNCATED "
                "(%d chars). Raise _COMPILER_MAX_OUTPUT_TOKENS or shrink the input.",
                _COMPILER_MAX_OUTPUT_TOKENS,
                len(content),
            )
            research_trace.emit(
                "brief",
                "compiler output TRUNCATED at output cap",
                content,
                status="truncated",
                error=f"finish_reason=length at max_tokens={_COMPILER_MAX_OUTPUT_TOKENS}",
                meta={"chain": "brief", "max_tokens": _COMPILER_MAX_OUTPUT_TOKENS},
            )
            content = content.rstrip() + _TRUNCATION_NOTICE
        return _normalise_compiled_report(content)
    except HardLimitExceededError:
        raise
    except Exception as exc:
        logger.warning("Research compiler LLM pass failed: %s: %s", type(exc).__name__, exc)
        return None


# Qwen-only additions to the brief prompt (QWEN_BRIEF_PROMPT=v2; the Opus path
# never sees them). Each rule targets a judgment failure seen in the 2026-09-21/22
# research A/B, written as a general rule: dropping a dated primary value as
# "conflicting", leaving out evidence present in the input, "no bound" on markets
# whose condition is entailed, one-sided or unreasoned Balance Checks, and
# missing named-institution projections toward the resolution date.
_QWEN_BRIEF_RULES = """
Additional selection rules (these refine the rules above; where they conflict, these win):
1. INVENTORY BEFORE SELECTING. Before writing, go through the whole input and note every
   candidate item that bears on the resolution value: the latest reading of the target
   and its date; readings of the same series at other dates; rules, schedules and
   mechanisms; projections or estimates by named institutions for dates at or near the
   resolution date; market prices; precedents from prior comparable cycles; base rates.
   Every candidate must end up either as an [E#] item or named, with its URL and a reason,
   under "Decision-relevant items EXCLUDED" in the Balance Check. Nothing decision-relevant
   may disappear silently.
2. A CONFLICT IS NOT A REASON TO EXCLUDE. When dated values of the same measure disagree
   or look inconsistent (for example an earlier value higher than a later one), keep them
   as separate items with their own dates and sources and state the discrepancy — movement
   in both directions is itself evidence about how the series behaves. Exclude a value only
   when its source is unreliable (unsourced, mislabeled, or contradicted by the
   authoritative source for the SAME date), and say which.
3. FORWARD-LOOKING ANCHORS. If the input contains dated projections, milestone forecasts,
   schedules or issuance/borrowing plans from named institutions that bear on the value at
   the resolution date, include the most relevant as items, with each projection's date and
   basis.
4. PRECEDENTS. If the input describes how the same event, market or series behaved in a
   prior comparable cycle, include the most relevant such precedent as an item even when it
   carries no number.
5. MARKET BOUNDS — CHECK BOTH DIRECTIONS. For each market whose condition differs from this
   question's, write one line of entailment before the verdict: if this question's outcome
   implies the market's event, the market price is a CEILING on that outcome's probability;
   if the market's event implies the outcome, it is a FLOOR. Example: question "Will team T
   win the final?", market "Will team T reach the final?" — winning requires reaching, so
   the market price is a ceiling on P(T wins). Write "no bound" only after checking both
   directions and finding neither holds.
6. BALANCE CHECK WITH REASONS. After each [E#] in the Balance Check, add a short clause
   saying why it points that way. An item may appear on only one side; if it cuts both
   ways, place it where it weighs most and say so.
""".rstrip()

# v3 = v2 with the wording that leaned on the questions v2 was developed on made
# neutral, so it can be tested on questions it has not seen: rule 1 no longer
# frames evidence as a numeric time series, rule 2's example is not the 45412
# month-end debt value, and rule 3 says "announced plans" instead of the fiscal
# "issuance/borrowing plans". Rules 4-6 are unchanged. v2 stays for comparison
# with the 2026-09-22 brief prompt A/B (data/brief-prompt-ab/results.md).
_QWEN_BRIEF_RULES_V3 = """
Additional selection rules (these refine the rules above; where they conflict, these win):
1. INVENTORY BEFORE SELECTING. Before writing, go through the whole input and note every
   candidate item that bears on the resolution: the most recent observed state of whatever
   the question resolves on, with its date; earlier observed states of the same thing;
   rules, schedules and mechanisms; projections or expectations by named institutions or
   experts for dates at or near the resolution date; market prices; precedents from prior
   comparable cycles; base rates. Every candidate must end up either as an [E#] item or
   named, with its URL and a reason, under "Decision-relevant items EXCLUDED" in the
   Balance Check. Nothing decision-relevant may disappear silently.
2. A CONFLICT IS NOT A REASON TO EXCLUDE. When sources disagree about the same fact —
   different figures, dates, statuses or outcomes for the same thing — keep each as a
   separate item with its own date and source and state the discrepancy; how the sources
   disagree is itself evidence. Exclude an item only when its source is unreliable
   (unsourced, mislabeled, or contradicted by the authoritative source for the SAME date),
   and say which.
3. FORWARD-LOOKING ANCHORS. If the input contains dated projections, forecasts, schedules,
   announced plans or official timelines from named institutions that bear on the outcome
   at the resolution date, include the most relevant as items, with each one's date and
   basis.
4. PRECEDENTS. If the input describes how the same event, market or series behaved in a
   prior comparable cycle, include the most relevant such precedent as an item even when it
   carries no number.
5. MARKET BOUNDS — CHECK BOTH DIRECTIONS. For each market whose condition differs from this
   question's, write one line of entailment before the verdict: if this question's outcome
   implies the market's event, the market price is a CEILING on that outcome's probability;
   if the market's event implies the outcome, it is a FLOOR. Example: question "Will team T
   win the final?", market "Will team T reach the final?" — winning requires reaching, so
   the market price is a ceiling on P(T wins). Write "no bound" only after checking both
   directions and finding neither holds.
6. BALANCE CHECK WITH REASONS. After each [E#] in the Balance Check, add a short clause
   saying why it points that way. An item may appear on only one side; if it cuts both
   ways, place it where it weighs most and say so.
""".rstrip()

_BRIEF_VARIANT_RULES = {"v2": _QWEN_BRIEF_RULES, "v3": _QWEN_BRIEF_RULES_V3}
_BRIEF_OUTPUT_ANCHOR = "\nOutput exactly these Markdown sections:"


def apply_brief_variant(prompt: str, variant: str) -> str:
    """Return the brief prompt for a named variant ("base" leaves it unchanged)."""
    if variant == "base":
        return prompt
    rules = _BRIEF_VARIANT_RULES.get(variant)
    if rules is None:
        raise ValueError(f"Unknown QWEN_BRIEF_PROMPT variant {variant!r}")
    if prompt.count(_BRIEF_OUTPUT_ANCHOR) != 1:
        raise ValueError("Brief prompt output anchor not found exactly once")
    return prompt.replace(_BRIEF_OUTPUT_ANCHOR, "\n" + rules + "\n" + _BRIEF_OUTPUT_ANCHOR, 1)


def _format_artifact_check(artifact_check: dict | None) -> str:
    if not artifact_check:
        return "No automated artifact check was run."
    lines = [
        f"Status: {artifact_check.get('status', 'unknown')}",
        f"What was found: {artifact_check.get('what_was_found') or 'Not stated.'}",
        f"What is missing: {artifact_check.get('what_is_missing') or 'Nothing noted.'}",
    ]
    closest_available = artifact_check.get("closest_available")
    if closest_available:
        lines.append(
            "Closest available adjacent metric (carry forward into Key Evidence UNLESS the "
            "scraped research explicitly corrects, redates, or refutes it — a correction found "
            "in the research outranks this line; in that case carry the CORRECTED fact instead "
            "and flag the discrepancy in Gaps And Cautions): "
            f"{closest_available}"
        )
    forecast_swing = artifact_check.get("forecast_swing")
    if forecast_swing:
        lines.append(
            f"Estimated forecast swing if the missing information were resolved: {forecast_swing}"
        )
    return "\n".join(lines)


def _build_compiler_prompt(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    cleaned_sections: list[ProviderResult],
    artifact_check: dict | None = None,
) -> str:
    resolution_sections = [
        (provider, content)
        for provider, content in cleaned_sections
        if "resolution" in provider.lower()
    ]
    other_sections = [
        (provider, content)
        for provider, content in cleaned_sections
        if "resolution" not in provider.lower()
    ]
    # Sections arrive already fitted to _COMPILER_INPUT_BUDGET_CHARS by
    # _fit_sections_to_budget (LLM compression, visible markers) — no [:N]
    # truncation happens here.
    resolution_text = _format_sections(resolution_sections) if resolution_sections else ""
    research_text = _format_sections(other_sections)

    today = datetime.date.today().isoformat()
    return f"""
Today's date is {today}.

Forecast question:
{title}

Resolution criteria:
{resolution_criteria or "Not provided."}

Background:
{background or "Not provided."}

Fine print:
{fine_print or "Not provided."}

Automated check of whether the required evidence artifact was found:
{_format_artifact_check(artifact_check)}

RESOLUTION SOURCE material (AUTHORITATIVE — this is the page/feed the question
resolves from; it outranks every secondary source below for the resolution value.
It was scraped DURING THIS RESEARCH RUN, i.e. on {today}; if the page displays a
data cutoff or "as of" date older than that, the gap between the two dates is
direct evidence of how often the source actually updates):
{resolution_text or "No resolution-source scrape was available for this question."}

Other research provider outputs, already partially cleaned (secondary; use to inform
the forecast, never to stand in for the resolution value):
{research_text}

Task:
Distill the raw research into a detailed evidence brief for a forecaster. Select
only what could plausibly change the forecast. Drop filler, vivid color, and
broad commentary that does not bear on the resolution criteria.

Consistency check (do this before selecting evidence). Cross-check the retrieved
items against each other and against the resolution source. Flag every failure in
"Gaps And Cautions" and never label a failing item `direct`:
- Temporally impossible evidence: today is {today}. A report or observation whose claimed event or publication date is AFTER today cannot exist — the date is wrong (almost always a prior-year event mislabeled with the current year). Reclassify it as misdated historical data, flag it, and NEVER present its value as a candidate for the resolution window. This rule outranks the automated artifact check above: if that check carries a future-dated claim, correct it here rather than repeating it.
- Corrections outrank earlier inferences: if any scrape-cycle extract explicitly corrects, redates, or retracts a claim made elsewhere in the research (e.g. "Important note: this event is dated 8 August 2025, not 2026"), the correction wins. Carry the corrected fact into the brief, drop the superseded claim, and flag the contamination in Gaps And Cautions.
- Year-less dates: never assume a date without a year ("Aug 7") falls in the current year or the resolution window; keep the item marked "(year not stated in source)" and do not label it `direct`.
- Same value, two dates: if an identical figure is attributed to two different periods (e.g. the same number reported for both 2025 and 2026), at least one date is wrong or it is one stale item double-counted — flag it and treat neither as confirmed current data.
- Contradicts the resolution series: if a figure conflicts with the resolution source's own table (a "latest" reading the resolution source does not show, or one out of order with its trajectory), trust the resolution source and flag the outlier.
- Impossible superlative: if a claim like "N-month high/low" is inconsistent with the values in the extracted series, flag it.
- Wrong-era drivers: if the reasons given for a supposedly current datapoint describe events from a different period, treat that datapoint's date as suspect.
- ALREADY IN THE BASELINE? When the resolution source shows a confirmed current value (a count, total, list, or standing "as of" some date), every evidence item that implies movement toward or away from that value must be reconciled against it: does the item describe something that happened BEFORE the baseline's "as of" date (its effect is already inside the current value) or AFTER it (a genuine pending change)? Say which on the item. Watch especially for follow-up coverage of an old event (implementing decrees, anniversary pieces, secondary rollouts of an already-enacted law) dressed as new movement. If the research does not let you place an entity inside or outside the current value (e.g. the source's row-level breakdown was not retrieved), the item must say "(position vs. baseline unverified)" and must NOT be presented as the strongest candidate to change the value — and flag the unretrieved breakdown in Gaps And Cautions as the blocking gap.

Output exactly these Markdown sections:

# Compiled Research Brief

## Extracted Artifact Rows
- Name the artifact the Evidence Plan says is most important.
- Do NOT write a found / partial / not-found verdict here — the authoritative artifact status is shown to the forecaster in a separate fixed banner above this brief. This section is only for the data itself.
- If it is a table or time series and any rows were extracted, reproduce those rows here verbatim, and mark the resolution-target row as "not yet released" when the resolution source does not show it. This is the single most important section.
- Never present a secondary or year-ago figure as if it were the confirmed resolution value.
- If the automated check lists a "Closest available adjacent metric", reproduce it here and carry it into Key Evidence as an `adjacent-metric` item. Never omit a value that was actually retrieved just because it is not the exact metric.

## Resolution Mechanics
Only when the question resolves off a published source (a curated page, tracker, leaderboard, or scheduled data release) rather than by direct observation of an event; if it resolves by direct observation, write the bullet "Not applicable — resolves by direct observation of the event" — and when that event is one step inside a longer causal sequence (steps that must precede it, steps that normally follow it), add one bullet writing out the chain in order with the resolution event marked (e.g. "confidential draft → review → PUBLIC FILING (resolution event) → roadshow → listing"), so the forecaster can classify each report and market as upstream or downstream of the resolution event. State the chain as the sources describe it; do not attach timing conclusions to it here. Otherwise, at most 4 bullets, each citing its evidence:
- Whether the resolution source will or may update again before the resolution deadline: stated cadence (e.g. "updated periodically"), scheduled releases, and the observed freshness gap. If the resolution-source scrape (fetched {today}) displays a data cutoff or "as of" date older than the fetch date, state both dates explicitly — that gap is direct update-cadence evidence. If the resolution material includes a "Resolution Source History" section (dated archive captures), carry its value time series and observed update cadence here — a same-source historical series is the ONLY valid basis for a flow rate; never let a cross-source coincidence stand in for one.
- What new information CAN appear in the source before the deadline, and what CANNOT arrive in time (reporting calendars, disclosure deadlines, publication or data-pipeline lags). Distinguish activity that will be observable by the deadline from activity that happens before the deadline but is disclosed only after it.
- Any scheduled data event between today and the deadline (filing deadline, release date) that would change what the source shows.
- If the research contains nothing on these mechanics, write one bullet saying exactly that — do not invent a cadence or calendar.

## Key Evidence
A list of at most 15 items. Do NOT sort by relevance — order does not matter, and the [E#] labels are just citation handles, not a priority ranking. Format each item as:
[E1] (tier) Claim with exact numbers and dates. — Source name, publish date, URL
- SELECT BY DECISION-RELEVANCE (this governs which items make the list, not their order). The items that must appear whenever the research supports them are: (1) the RULE or MECHANISM that governs how the resolution value changes over time — eligibility criteria, recovery/transition conditions, the clock or event that triggers a change, a reaction function, a scheduled decision; and (2) the CURRENT VALUE of each input that rule depends on (including the date that starts the clock, not just the date a change was announced). A precise figure that does not feed this mechanism is background color, however exact. When a number's relevance hinges on a condition (a confounder that speeds or slows the mechanism), keep the condition with the number.
- OBSERVED BEHAVIOR OUTRANKS FORMAL PROCESS: when the question resolves on an observed event or action, a reported instance of that same behavior actually happening (a precedent, a completed prior step, a dry run) is top-tier evidence even if it carries no number and sits outside the formal process the documents describe. Do not drop a behavioral precedent in favor of one more restatement of the process rules.
- DIRECTIONAL BALANCE (required): after drafting the list, check it as a whole. Include the strongest items pointing EACH way that the research supports — toward YES and toward NO for a binary question; toward higher and lower values otherwise. If the raw research contains a plausibly decision-relevant item pointing against the majority of your list and you have excluded it, that is a selection error: include it. A lopsided list is acceptable ONLY when the research itself contains no credible opposing items — in that case say so explicitly in the Balance Check section below. Do not manufacture balance that the research does not contain; the requirement is that no side's strongest evidence is silently dropped.
- OBSERVATIONS ONLY: every item must be something a source actually states, shows, or prices — a fact, quote, measurement, market price, or the documented rule/mechanism itself. Never emit YOUR OWN inference, extrapolation, or timeline arithmetic (e.g. "the October target implies a September filing") as an [E#] item, even attributed to an evidence plan or labelled "synthesis" — an inference wearing an [E#] label acquires the authority of evidence and every downstream forecaster will cite it as fact. Put such reasoning in ## Derived Implications instead.
- tier is one of: direct (measures the resolution target itself, from the resolution source or confirmed equal to it), adjacent-metric (same family but a different basis/series; state the relationship and any conversion toward the target), near-proxy (close but not identical; say in a few words why not identical), market (prediction-market signal).
- Every item must carry the observation date/period of its value. If a value's date cannot be tied to the period the question asks about, append "(date unverified)" and do NOT label it `direct` — a value reported by a single article without a confirmable current date is not direct evidence.
- Keep exact values, dates, counts, and odds. Never round away precision present in the source.
- When several articles report the same fact (syndicated or near-identical coverage), output ONE item and list every source/URL on that item. Do not repeat the fact.
- End every item with a source-document tag [D1], [D2], ...: items whose claims trace to the same underlying document, report, or dataset share one tag even when they cover different facts or arrive via different URLs (e.g. four extracts from one policy brief are all [D2]). The forecaster uses these tags to weight corroboration by unique sources.
- Exclude weak proxies and background color entirely unless fewer than 5 stronger items exist. A statement of the governing rule/mechanism (or a condition that materially speeds or slows it) is never background color — keep it even if it carries no number of its own.
- Do not place the same fact in more than one item.

## Balance Check
Exactly these three lines, filled in (required — the forecaster reads them; this is the audit trail proving no direction's evidence was dropped):
- Strongest evidence FOR the event / higher values: [E#, E#, ...] — or "none found in the research"
- Strongest evidence AGAINST the event / lower values: [E#, E#, ...] — or "none found in the research"
- Decision-relevant items EXCLUDED from Key Evidence: one short clause each with source and URL (e.g. "LAF entered vacated positions at X — site.com/url — excluded as single-sourced"); write "none" only if nothing plausibly decision-relevant was left out.

## Derived Implications
Omit this section entirely if you have none. Otherwise at most 3 bullets, labelled [I1], [I2], ... — NEVER [E#]. Each is an inference you draw by chaining evidence items (e.g. working a deadline backwards through an interval rule), and must (a) cite the [E#] items it chains and (b) name every load-bearing assumption inline — especially any assumption that a stated minimum, earliest, or latest bound is also the TYPICAL case (e.g. "[I1] From [E3]+[E5], assuming the statutory MINIMUM 15-day gap is also the typical gap — not established by the evidence — an October listing implies a September public filing"). The forecaster re-derives these from the underlying [E#] items; an [I#] is a hypothesis to check, not evidence to cite.

## Market Signals
- One bullet per relevant market: question, current odds, volume/liquidity/open interest when present, URL. Real-money markets (Polymarket, Kalshi) before play-money (Manifold).
- For every market, state in the same bullet whether its resolution condition MATCHES this question's or DIFFERS (broader/narrower/different event). A differing market is a directional floor or ceiling only — and the direction must be DERIVED BY ENTAILMENT, shown in the bullet: if the market's event requires this question's event to happen first (it sits downstream of the resolution event), its price is a FLOOR on this question; if this question's event requires the market's event, a CEILING; if neither entailment holds, write "no bound — context only". Never assert floor/ceiling without the entailment — a misassigned direction inverts how every forecaster uses the market.
- If no market bears directly on the question, say so in one bullet and do not pad with adjacent markets.

## Gaps And Cautions
- At most 6 bullets: missing facts, stale data, conflicting reports, resolution-source access failures, and revision risks.
- If the required artifact is missing or partial, say what that implies for forecast uncertainty in one bullet.

Rules:
- Do not make a probability estimate.
- Do not invent facts absent from the raw research.
- The RESOLUTION SOURCE material is authoritative. When it reports a value for the resolution target, it overrides any secondary source that disagrees. When it shows the target as blank, unpublished, or not-yet-released, say so explicitly and NEVER substitute a secondary or year-ago figure as the resolved value — secondary figures may inform the forecast but are not the resolution value.
- Do not delete a value that was actually retrieved. If it is the wrong exact metric but a related one, keep it as an `adjacent-metric` item with its caveat and conversion path; "not extracted" is only for values that were never found.
- Total output should be materially shorter than the input. Selectivity is the job.
""".strip()


def _build_heuristic_report(
    title: str,
    resolution_criteria: str,
    sections: list[ProviderResult],
    artifact_check: dict | None = None,
) -> str:
    key_evidence = _select_key_evidence(title, resolution_criteria, sections)
    market_sections = [
        content for provider, content in sections
        if provider.lower() in {"kalshi", "manifold", "polymarket"}
    ]
    evidence_plan_sections = [
        content for provider, content in sections
        if provider.lower() == "evidence plan"
    ]
    resolution_sections = [
        content for provider, content in sections
        if "resolution" in provider.lower()
    ]
    news_sections = [
        content for provider, content in sections
        if "asknews" in provider.lower()
    ]
    other_sections = [
        (provider, content)
        for provider, content in sections
        if provider.lower() not in {"kalshi", "manifold", "polymarket", "asknews", "evidence plan"}
        and "resolution" not in provider.lower()
    ]

    # The authoritative artifact status is injected as a fixed banner by the
    # pipeline (_apply_artifact_status_banner) on both the LLM and heuristic paths,
    # so this fallback report does not emit its own status section (avoids a
    # duplicate block).
    lines = ["# Compiled Research Brief", ""]
    lines += ["## Key Facts And Evidence"]
    if key_evidence:
        lines.extend(f"- {item}" for item in key_evidence)
    else:
        lines.append("- No high-signal evidence could be extracted from the research providers.")

    lines.extend(["", "## Required Evidence Artifact"])
    if evidence_plan_sections:
        lines.append(_join_compact(evidence_plan_sections, max_chars=5_000))
    else:
        lines.append("- No evidence plan was available.")

    lines.extend(["", "## Direct Evidence"])
    if resolution_sections:
        lines.append(_join_compact(resolution_sections, max_chars=5_000))
    else:
        lines.append("- No direct resolution-source evidence was available.")

    lines.extend(["", "## Near Proxy Evidence"])
    lines.append("- Review market and scraped-search sections below for close-but-not-identical evidence.")

    lines.extend(["", "## Weak Proxy Evidence"])
    lines.append("- Treat adjacent markets, broad commentary, and indirect technical/context signals cautiously unless they directly match the resolution criteria.")

    lines.extend(["", "## Background Color"])
    if news_sections:
        lines.append("- AskNews and general scraped evidence may provide useful context, but should not override missing direct base-rate artifacts.")
    else:
        lines.append("- No background news context was available.")

    lines.extend(["", "## Market Signals"])
    if market_sections:
        lines.append(_join_compact(market_sections, max_chars=8_000))
    else:
        lines.append("- No useful Polymarket, Kalshi, or Manifold signal found.")

    lines.extend(["", "## Resolution Source Findings"])
    if resolution_sections:
        lines.append(_join_compact(resolution_sections, max_chars=10_000))
    else:
        lines.append("- No resolution-source scrape was available or no URL was present in the resolution criteria.")

    lines.extend(["", "## News And External Evidence"])
    if news_sections:
        lines.append(_join_compact(news_sections, max_chars=14_000))
    else:
        lines.append("- No AskNews articles were available.")

    if other_sections:
        lines.extend(["", "## Other Provider Output"])
        for provider, content in other_sections:
            lines.extend([f"### {provider}", _truncate_text(content, 4_000)])

    lines.extend(
        [
            "",
            "## Uncertainties And Gaps",
            "- Check whether any newer official source has appeared since the research was fetched.",
            "- Repeated or syndicated article bodies are grouped above; review the full citation list for source breadth.",
            "- Discount thin prediction markets relative to high-volume, high-liquidity markets.",
        ]
    )
    return _normalise_compiled_report("\n".join(lines))


def _select_key_evidence(
    title: str,
    resolution_criteria: str,
    sections: list[ProviderResult],
) -> list[str]:
    keywords = _extract_keywords(f"{title}\n{resolution_criteria}")
    scored_items: list[tuple[float, str]] = []
    seen: set[str] = set()

    for provider, content in sections:
        provider_bonus = 2.0 if "resolution" in provider.lower() else 0.0
        provider_bonus += 1.0 if provider.lower() in {"polymarket", "kalshi", "manifold"} else 0.0
        for item in _candidate_evidence_items(content):
            normalised = _normalise_for_dedupe(item)
            if not normalised or normalised in seen:
                continue
            seen.add(normalised)
            score = _score_evidence_item(item, keywords) + provider_bonus
            if score >= 4.0:
                scored_items.append((score, item))

    scored_items.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored_items[:_MAX_KEY_EVIDENCE_ITEMS]]


def _candidate_evidence_items(content: str) -> list[str]:
    items: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip(" -\t")
        line = re.sub(r"^\d+\.\s+", "", line)
        if not line or len(line) < 35:
            continue
        lowered = line.lower()
        if line.startswith("#") or lowered.startswith((
            "article content group:",
            "articles visited",
            "asknews articles",
            "citation:",
            "content:",
            "note:",
        )):
            continue
        if " | published " in lowered and " | source " in lowered:
            continue
        if len(line) <= 360:
            items.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line)
        items.extend(sentence.strip() for sentence in sentences if 50 <= len(sentence.strip()) <= 360)
    return items


def _score_evidence_item(item: str, keywords: set[str]) -> float:
    lowered = item.lower()
    score = 0.0
    if _DATE_HINT.search(item):
        score += 2.5
    if _NUMBER_HINT.search(item):
        score += 2.0
    if _URL_PATTERN.search(item):
        score += 0.5
    if any(word in lowered for word in ("confirmed", "reported", "announced", "published", "official", "authority", "who ", "cdc", "ecdc")):
        score += 2.0
    if any(word in lowered for word in ("odds", "probability", "volume", "liquidity", "relevance score")):
        score += 1.5
    if any(word in lowered for word in ("not found", "no useful", "unavailable", "failed")):
        score -= 2.0
    keyword_hits = sum(1 for keyword in keywords if keyword in lowered)
    score += min(keyword_hits, 6) * 0.7
    return score


def _extract_keywords(text: str) -> set[str]:
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
        if token.lower() not in _STOPWORDS
    }
    return tokens


def _format_sections(sections: list[ProviderResult]) -> str:
    # No truncation: callers hand in sections already fitted to the compiler
    # input budget by _fit_sections_to_budget. This function used to apply the
    # 24K/60K [:N] cuts that caused the 44619 miss (all YES-leaning evidence
    # sat past the cut point of one 116K-char section).
    return "\n\n---\n\n".join(
        f"## Provider: {provider}\n{content}" for provider, content in sections
    )


def _join_compact(parts: list[str], max_chars: int) -> str:
    return _truncate_text("\n\n".join(part.strip() for part in parts if part.strip()), max_chars)




def _normalise_compiled_report(text: str) -> str:
    text = _clean_generic_text(text)
    if not text.startswith("# Compiled Research Brief"):
        text = "# Compiled Research Brief\n\n" + text
    return text


def _normalise_for_dedupe(text: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", text.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _collapse_spaces(text)


def _collapse_spaces(text: str) -> str:
    return _WHITESPACE.sub(" ", text or "").strip()
