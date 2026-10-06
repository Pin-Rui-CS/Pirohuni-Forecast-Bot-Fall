from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import heapq
import itertools
import logging
import math
import os
import time
import traceback

from artifacts import QuestionArtifacts
from forecast_comment import short_comment
import llm_provider
from config import API_BASE_URL, OPENROUTER_API_KEY
from forecasters.base import ForecastResult
from forecasters.binary import get_binary_gpt_prediction
from forecasters.multiple_choice import get_multiple_choice_gpt_prediction
from forecasters.numeric import get_numeric_gpt_prediction
from metaculus_client import (
    create_forecast_payload,
    get_post_details,
    post_tournaments,
    post_question_comment,
    post_question_prediction,
)
from monetary_cost_manager import (
    HardLimitExceededError,
    MonetaryCostManager,
    get_openrouter_key_usage,
    research_reserve_input_tokens,
)
from research.pipeline import run_research
import qwen_ladder
import research_trace

logger = logging.getLogger(__name__)

QUESTION_TIMEOUT_SECONDS = int(os.getenv("QUESTION_TIMEOUT_SECONDS", str(20 * 60)))
# How many questions may be in flight at once. This used to be unbounded: a
# 35-question tournament started 35 questions simultaneously, each fanning out
# to its own research providers. Nothing downstream could meaningfully throttle
# that -- the LLM semaphore caps concurrent *calls*, not questions, and the
# synchronous market providers run in worker threads that bypass it entirely.
# The cost is invisible while every endpoint is elastic and becomes decisive
# the moment one is rate-limited, because QUESTION_TIMEOUT_SECONDS is measured
# from question start and so is spent while merely queueing.
# 0 restores the old unbounded behaviour.
QUESTION_CONCURRENCY = int(os.getenv("QUESTION_CONCURRENCY", "0"))
# The GitHub-hosted job limit (no timeout-minutes is set, so 360 min). No
# question is started later than this minus QUESTION_TIMEOUT_SECONDS; what is
# left waits for the next run instead of being killed mid-forecast.
RUN_TIME_LIMIT_SECONDS = int(os.getenv("RUN_TIME_LIMIT_SECONDS", str(6 * 3600)))
# Live queue: how often a run may re-list its tournaments for newly opened
# questions (it does so only when a worker frees up).
QUESTION_DISCOVERY_INTERVAL_SECONDS = float(os.getenv("QUESTION_DISCOVERY_INTERVAL_SECONDS", "60"))

# Starts the summary line of a question whose research lost SoCLaaS Qwen;
# forecast_questions counts these so an outage is never only a log line.
QWEN_OUTAGE_MARKER = "QWEN OUTAGE:"

_QUESTION_SNAPSHOT_KEYS = (
    "id",
    "title",
    "type",
    "resolution_criteria",
    "description",
    "fine_print",
    "options",
    "scaling",
    "open_upper_bound",
    "open_lower_bound",
    "unit",
    "status",
    "scheduled_close_time",
    "scheduled_resolve_time",
)


async def get_openrouter_usage_summary() -> str:
    # OpenRouter exposes remaining credit on /key. The OpenAI API has no
    # equivalent: its Costs API is daily-bucketed and needs a separate admin
    # key, so per-run spend there comes from the local price table in the
    # usage ledger instead (llm_provider.PRICES).
    if llm_provider.is_openai():
        return (
            "Key usage summary unavailable on the OpenAI API (no per-key credit "
            "endpoint) — see total_cost_usd in the usage ledger, computed from "
            "llm_provider.PRICES."
        )
    try:
        data = await get_openrouter_key_usage(OPENROUTER_API_KEY or "")
    except Exception as exc:
        return (
            "OpenRouter key usage unavailable: "
            f"{exc.__class__.__name__}: {exc}"
        )

    label = data.get("label") or "current key"
    limit_remaining = data.get("limit_remaining")
    key_limit = data.get("limit")
    usage = data.get("usage")
    usage_daily = data.get("usage_daily")

    if limit_remaining is None:
        remaining_text = "unlimited or no key limit configured"
    else:
        remaining_text = f"${float(limit_remaining):.6f}"

    limit_text = "unlimited" if key_limit is None else f"${float(key_limit):.6f}"
    usage_text = "unknown" if usage is None else f"${float(usage):.6f}"
    daily_text = "unknown" if usage_daily is None else f"${float(usage_daily):.6f}"
    return (
        f"OpenRouter key usage ({label}): "
        f"limit_remaining={remaining_text}, limit={limit_text}, "
        f"usage={usage_text}, usage_daily={daily_text}"
    )


def forecast_is_already_made(post_details: dict) -> bool:
    """
    Check if a forecast has already been made by looking at my_forecasts in the question data.

    question.my_forecasts.latest.forecast_values has the following values for each question type:
    Binary: [probability for no, probability for yes]
    Numeric: [cdf value 1, cdf value 2, ..., cdf value 201]
    Multiple Choice: [probability for option 1, probability for option 2, ...]
    """
    question_details = post_details.get("question") or {}
    my_forecasts = question_details.get("my_forecasts") or {}
    latest_forecast = my_forecasts.get("latest") or {}
    forecast_values = latest_forecast.get("forecast_values")
    return forecast_values is not None


def _question_snapshot(question_details: dict) -> dict:
    return {
        key: question_details.get(key)
        for key in _QUESTION_SNAPSHOT_KEYS
        if question_details.get(key) is not None
    }


def _fetch_budget_snapshot(scope: str) -> dict[str, object]:
    """Paid-fetch counters for audit.md. Best-effort; never fails a run."""
    try:
        from config import (
            ENABLE_FIRECRAWL_GENERAL_SCRAPE,
            FIRECRAWL_QUESTION_CREDIT_CAP,
        )
        from research.firecrawl_scrape import firecrawl_credits_spent, firecrawl_exhausted

        return {
            "firecrawl_credits_spent": firecrawl_credits_spent(scope),
            "firecrawl_credit_cap": FIRECRAWL_QUESTION_CREDIT_CAP,
            "firecrawl_exhausted": firecrawl_exhausted(),
            "general_scrape_enabled": ENABLE_FIRECRAWL_GENERAL_SCRAPE,
        }
    except Exception as exc:  # noqa: BLE001 - reporting must never break a run
        logger.warning(
            "could not snapshot the fetch budget: %s: %s", type(exc).__name__, exc
        )
        return {}


async def forecast_individual_question(
    question_id: int,
    post_id: int,
    submit_prediction: bool,
    num_runs_per_question: int,
    skip_previously_forecasted_questions: bool,
    per_question_token_hard_limit: float = 0,
) -> str:
    import source_ledger
    from Crawl4AI.crawl import set_scrape_dedupe_scope

    scope = f"question:{question_id}:{post_id}"
    set_scrape_dedupe_scope(scope)
    source_ledger.set_source_scope(scope)
    post_details = await get_post_details(post_id)
    question_details = post_details.get("question")
    if not isinstance(question_details, dict):
        raise ValueError(f"Post {post_id} has no question payload")

    title = question_details.get("title") or post_details.get("title") or "Untitled"
    question_type = question_details.get("type")
    question_status = question_details.get("status")

    summary_of_forecast = ""
    summary_of_forecast += (
        f"-----------------------------------------------\nQuestion: {title}\n"
    )
    summary_of_forecast += (
        f"Post ID: {post_id}\nQuestion ID: {question_id}\n"
        f"Post API URL: {API_BASE_URL}/posts/{post_id}/\n"
    )

    if question_status != "open":
        summary_of_forecast += f"Skipped: Question status is {question_status!r}, not 'open'.\n"
        return summary_of_forecast

    if question_type == "multiple_choice":
        options = question_details.get("options") or []
        summary_of_forecast += f"options: {options}\n"

    if forecast_is_already_made(post_details) and skip_previously_forecasted_questions:
        summary_of_forecast += "Skipped: Forecast already made\n"
        return summary_of_forecast

    artifacts = QuestionArtifacts(question_id, post_id, title, question_type or "unknown")
    research_trace.begin_question(artifacts.dir)
    question_started = time.monotonic()
    # Records SoCLaaS going down during this question: once a Qwen call and its
    # retry both fail, later Qwen calls fail at once (Q46024, 2026-10-01).
    outage_tally = qwen_ladder.track_outages()
    qwen_outage_note = ""

    # The reserve keeps optional research work (extra scrape cycles, provider
    # fall-through, artifact retry) from spending the input budget that the
    # mandatory compile + forecast tail needs (44773 incident, 2026-07-16).
    with MonetaryCostManager(
        hard_limit=per_question_token_hard_limit,
        reserved_input_tokens=research_reserve_input_tokens(num_runs_per_question),
    ) as question_cost_manager:
        research_bundle = await run_research(
            title=question_details["title"],
            resolution_criteria=question_details["resolution_criteria"],
            background=question_details["description"],
            fine_print=question_details["fine_print"],
            question_type=question_type or "",
            # Enables the same-calendar-window block in a measured series.
            target_date=question_details.get("scheduled_resolve_time") or "",
        )
        research_seconds = time.monotonic() - question_started
        artifacts.save_research(
            evidence_plan=research_bundle.evidence_plan,
            provider_results=research_bundle.provider_results,
            compiled_report=research_bundle.compiled_report,
            artifact_check=research_bundle.artifact_check,
        )

        # No backup: the forecast goes ahead on whatever research survived,
        # flagged so the outage is visible in the summary and forecast.json.
        if outage_tally.down:
            qwen_outage_note = (
                f"{QWEN_OUTAGE_MARKER} SoCLaaS went down during research (failed after retry: "
                f"{', '.join(outage_tally.labels)}; skipped afterwards: "
                f"{', '.join(outage_tally.skipped) or 'none'}); forecasting on degraded research."
            )
            logger.warning("Question %s: %s", question_id, qwen_outage_note)

        forecast_started = time.monotonic()
        if question_type == "binary":
            result: ForecastResult = await get_binary_gpt_prediction(
                question_details,
                num_runs_per_question,
                research_bundle.compiled_report,
                raw_research=research_bundle.raw_research_view,
            )
        elif question_type in ("numeric", "discrete"):
            result = await get_numeric_gpt_prediction(
                question_details,
                num_runs_per_question,
                research_bundle.compiled_report,
                raw_research=research_bundle.raw_research_view,
            )
        elif question_type == "multiple_choice":
            result = await get_multiple_choice_gpt_prediction(
                question_details,
                num_runs_per_question,
                research_bundle.compiled_report,
                raw_research=research_bundle.raw_research_view,
            )
        else:
            raise ValueError(f"Unknown question type: {question_type}")
        forecast_seconds = time.monotonic() - forecast_started
        total_seconds = time.monotonic() - question_started
        estimated_tokens = question_cost_manager.total_tokens
        usage_yaml_table = question_cost_manager.format_usage_yaml_table()
        # The same ledger as rows, so the forecast library can query cost per
        # call without parsing the rendered table above.
        llm_calls = [asdict(record) for record in question_cost_manager.get_usage_records()]
        total_cost_usd = question_cost_manager.total_cost_usd

    timings = {
        "research_seconds": round(research_seconds, 1),
        "forecast_seconds": round(forecast_seconds, 1),
        "total_seconds": round(total_seconds, 1),
    }
    logger.info(
        "Question %s wall time: research %.1fs + forecast %.1fs = %.1fs total",
        question_id,
        research_seconds,
        forecast_seconds,
        total_seconds,
    )

    logger.info(
        "-----------------------------------------------\nPost %s Question %s:",
        post_id,
        question_id,
    )
    logger.info("Forecast for post %s (question %s): %s", post_id, question_id, str(result.forecast)[:200])

    if question_type in ("numeric", "discrete"):
        summary_of_forecast += f"Forecast: {str(result.forecast)[:200]}...\n"
    else:
        summary_of_forecast += f"Forecast: {result.forecast}\n"

    summary_of_forecast += f"Comment:\n```\n{result.comment[:200]}...\n```\n\n"

    # Surface silent ensemble degradation: a run refused by the token budget or
    # dropped on a provider failure otherwise leaves only a buried log line
    # (the 44773 "successful" run lost its raw-view member invisibly at
    # 245K/250K input tokens).
    completed_runs = len(result.run_values or [])
    if completed_runs < num_runs_per_question:
        degradation_line = (
            f"ENSEMBLE DEGRADED: {completed_runs}/{num_runs_per_question} forecast "
            "runs completed (see runs.md / logs for the dropped member)"
        )
        logger.warning("Question %s: %s", question_id, degradation_line)
        summary_of_forecast += f"{degradation_line}\n"

    if qwen_outage_note:
        summary_of_forecast += f"{qwen_outage_note}\n"

    summary_of_forecast += (
        f"Wall time: research {research_seconds:.1f}s + forecast {forecast_seconds:.1f}s "
        f"= {total_seconds:.1f}s total\n"
    )
    summary_of_forecast += f"Estimated OpenRouter LLM tokens: {estimated_tokens}\n"
    summary_of_forecast += f"{usage_yaml_table}\n"

    # A forecaster may abstain (forecast is None) when it has no usable signal —
    # e.g. every numeric run landed off the grid. Record the run for inspection
    # but never submit a non-forecast to Metaculus.
    abstained = result.forecast is None
    forecast_payload = create_forecast_payload(result.forecast, question_type)

    # What gets POSTED is a short summary of result.comment (the full rationale
    # of every run, often 10-20 KB). Cosmetic: built after the forecast is final,
    # free Qwen only, falls back to an excerpt, never fails the question.
    posted_comment = ""
    if submit_prediction and not abstained:
        posted_comment = await short_comment(
            title=title,
            question_type=question_type or "",
            forecast=result.forecast,
            full_comment=result.comment,
        )

    artifacts.save_runs(
        prompt=result.prompt,
        run_sections=result.run_transcripts,
        final_summary=result.comment,
        posted_comment=posted_comment,
    )
    artifacts.save_audit(
        usage_yaml_table=usage_yaml_table,
        url_events=source_ledger.drain_events(scope),
        fetch_budget=_fetch_budget_snapshot(scope),
    )
    research_trace.finalize()
    artifacts.save_forecast_json(
        {
            "question_details": _question_snapshot(question_details),
            # Additive, no schema_version bump: readers treat a missing key as unknown.
            "tournaments": post_tournaments(post_details),
            "artifact_check": research_bundle.artifact_check,
            "degraded_search_providers": research_bundle.degraded_search_providers,
            "qwen_outage": qwen_outage_note or None,
            "run_values": result.run_values,
            "final_forecast": result.forecast,
            "forecast_payload": forecast_payload,
            "extra": result.extra,
            "estimated_tokens": estimated_tokens,
            "total_cost_usd": total_cost_usd,
            "timings": timings,
            "usage_yaml_table": usage_yaml_table,
            "llm_calls": llm_calls,
            "abstained": abstained,
            "submitted": submit_prediction and not abstained,
            "posted_comment": posted_comment or None,
        }
    )

    if abstained:
        logger.warning(
            "Abstaining on post %s (question %s): no usable forecast; nothing submitted.",
            post_id, question_id,
        )
        summary_of_forecast += "Abstained: no usable forecast; nothing submitted.\n"
    elif submit_prediction:
        await post_question_prediction(question_id, forecast_payload)
        if posted_comment:
            await post_question_comment(post_id, posted_comment)
        summary_of_forecast += "Posted: Forecast was posted to Metaculus.\n"

    return summary_of_forecast


async def forecast_individual_question_with_timeout(
    question_id: int,
    post_id: int,
    submit_prediction: bool,
    num_runs_per_question: int,
    skip_previously_forecasted_questions: bool,
    per_question_token_hard_limit: float = 0,
    timeout_seconds: int = QUESTION_TIMEOUT_SECONDS,
) -> str:
    try:
        return await asyncio.wait_for(
            forecast_individual_question(
                question_id,
                post_id,
                submit_prediction,
                num_runs_per_question,
                skip_previously_forecasted_questions,
                per_question_token_hard_limit,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"Question {question_id} (post {post_id}) timed out after "
            f"{timeout_seconds // 60} minutes"
        ) from exc


@dataclass(order=True)
class _QueuedQuestion:
    """One question waiting for a worker. Sorts by tournament rank (CLI order:
    the main tournament first), then soonest close, then arrival."""

    rank: int
    close_ts: float
    seq: int
    question_id: int = field(compare=False)
    post_id: int = field(compare=False)
    attempt: int = field(default=1, compare=False)


def _close_timestamp(close_time: str | None) -> float:
    try:
        return datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return math.inf


async def forecast_questions(
    open_question_id_post_id: list[tuple[int, int]],
    submit_prediction: bool,
    num_runs_per_question: int,
    skip_previously_forecasted_questions: bool,
    per_question_token_hard_limit: float = 0,
    *,
    tournament_ids: list[int | str] | None = None,
) -> None:
    """Forecast a queue of questions with QUESTION_CONCURRENCY workers.

    2026-10-05: ~10 questions landed together (1 main tournament + minibench).
    The main one (46056) crashed mid-run, was never retried, and the next run
    could only start after this one finished, by which time it had closed. So:

    - the queue is ordered by tournament rank (``tournament_ids`` in CLI order,
      main tournament first), then soonest ``scheduled_close_time``;
    - with ``tournament_ids``, the tournaments are re-listed whenever a worker
      frees up (at most every QUESTION_DISCOVERY_INTERVAL_SECONDS), so a
      question released mid-run joins this run's queue at its priority instead
      of waiting for the next run;
    - a question that raises is queued once more at its priority, if still open;
    - no question is STARTED after RUN_TIME_LIMIT_SECONDS minus
      QUESTION_TIMEOUT_SECONDS, so the run ends inside the job limit.
    """
    # Self-initialize logging so entry points that call forecast_questions
    # directly (e.g. the inline-Python CI workflows) still get console INFO
    # output — including the per-question token/cost usage tables — and the
    # run.log file. No-op when forecasting_bot.py already configured it.
    from artifacts import run_log_file_path
    from metaculus_client import list_open_questions
    from run_logging import setup_run_logging

    setup_run_logging(run_log_file_path())

    logger.info(await get_openrouter_usage_summary())

    run_started = time.monotonic()
    start_deadline = run_started + RUN_TIME_LIMIT_SECONDS - QUESTION_TIMEOUT_SECONDS
    queue: list[_QueuedQuestion] = []
    known: set[int] = set()
    arrival = itertools.count()
    results: list[tuple[_QueuedQuestion, str | BaseException]] = []
    first_failures: list[tuple[_QueuedQuestion, BaseException]] = []

    def enqueue(question_id: int, post_id: int, rank: int, close_time: str | None) -> bool:
        if question_id in known:
            return False
        known.add(question_id)
        heapq.heappush(queue, _QueuedQuestion(
            rank, _close_timestamp(close_time), next(arrival), question_id, post_id))
        return True

    def discover() -> list[tuple[int, int, int, str | None]]:
        found = []
        for rank, tournament_id in enumerate(tournament_ids or []):
            for question in list_open_questions(tournament_id):
                found.append((question.question_id, question.post_id, rank,
                              question.scheduled_close_time))
        return found

    discovery_lock = asyncio.Lock()
    last_discovery = [-math.inf]

    async def refresh(force: bool = False) -> None:
        if not tournament_ids:
            return
        async with discovery_lock:
            if not force and time.monotonic() - last_discovery[0] < QUESTION_DISCOVERY_INTERVAL_SECONDS:
                return
            last_discovery[0] = time.monotonic()
            try:
                found = await asyncio.to_thread(discover)
            except Exception as exc:  # noqa: BLE001 - a listing failure keeps the current queue
                logger.warning("Re-listing tournaments failed (%s: %s); keeping the current queue.",
                               type(exc).__name__, exc)
                return
            added = [qid for qid, pid, rank, close in found if enqueue(qid, pid, rank, close)]
            if added and not force:
                logger.info("Live queue: %d newly opened question(s) joined this run: %s",
                            len(added), added)

    await refresh(force=True)
    # Explicit questions (cup workflows, examples, eval tools) keep their order
    # after any tournament-listed ones.
    for question_id, post_id in open_question_id_post_id:
        enqueue(question_id, post_id, len(tournament_ids or []), None)

    with MonetaryCostManager(
        input_token_hard_limit=0,
        output_token_hard_limit=0,
    ) as run_cost_manager:
        workers: set[asyncio.Task] = set()

        async def worker() -> None:
            while queue:
                if time.monotonic() > start_deadline:
                    logger.warning(
                        "Not starting more questions: %d left for the next run "
                        "(RUN_TIME_LIMIT_SECONDS=%d, QUESTION_TIMEOUT_SECONDS=%d).",
                        len(queue), RUN_TIME_LIMIT_SECONDS, QUESTION_TIMEOUT_SECONDS,
                    )
                    queue.clear()
                    return
                item = heapq.heappop(queue)
                # Own task per question: its ContextVars (trace, ledger, outage
                # tally) stay isolated, as they were under gather. The timeout
                # starts here, not while the question waited for a worker.
                try:
                    summary = await asyncio.create_task(forecast_individual_question_with_timeout(
                        item.question_id, item.post_id, submit_prediction, num_runs_per_question,
                        skip_previously_forecasted_questions, per_question_token_hard_limit,
                    ))
                except Exception as exc:  # noqa: BLE001 - recorded and retried below
                    retry = (
                        item.attempt == 1
                        and not isinstance(exc, HardLimitExceededError)
                        and item.close_ts > datetime.now(timezone.utc).timestamp()
                    )
                    if retry:
                        logger.warning(
                            "Question %s failed (%s: %s); queued once more at its priority.",
                            item.question_id, type(exc).__name__, exc,
                        )
                        first_failures.append((item, exc))
                        item.attempt = 2
                        item.seq = next(arrival)
                        heapq.heappush(queue, item)
                    else:
                        results.append((item, exc))
                else:
                    results.append((item, summary))
                await refresh()
                top_up()

        def top_up() -> None:
            limit = QUESTION_CONCURRENCY if QUESTION_CONCURRENCY > 0 else len(workers) + len(queue)
            while queue and len(workers) < limit:
                task = asyncio.create_task(worker())
                workers.add(task)
                task.add_done_callback(workers.discard)

        logger.info(
            "Forecasting %d question(s), at most %s at a time%s.",
            len(queue), QUESTION_CONCURRENCY or "all",
            "; new questions join the queue as they open" if tournament_ids else "",
        )
        top_up()
        while workers:
            await asyncio.wait(set(workers))
        total_estimated_tokens = run_cost_manager.total_tokens
        run_usage_yaml_table = run_cost_manager.format_usage_yaml_table("openrouter_llm_run_usage")

    for item, exc in first_failures:
        logger.warning("Question %s failed on its first attempt: %s: %s",
                       item.question_id, type(exc).__name__, exc)
    open_question_id_post_id = [(item.question_id, item.post_id) for item, _ in results]
    forecast_summaries = [outcome for _, outcome in results]

    completed_count = sum(
        1 for forecast_summary in forecast_summaries if not isinstance(forecast_summary, Exception)
    )
    average_estimated_tokens = (
        total_estimated_tokens / completed_count if completed_count else 0
    )
    logger.info("\n%s\nForecast Summaries\n%s", "#" * 100, "#" * 100)
    logger.info("Total estimated OpenRouter LLM tokens: %s", total_estimated_tokens)
    logger.info(
        "Average estimated tokens per completed question: %.1f", average_estimated_tokens
    )
    logger.info(run_usage_yaml_table)
    logger.info(await get_openrouter_usage_summary())

    degraded = [
        question_id
        for (question_id, _), forecast_summary in zip(open_question_id_post_id, forecast_summaries)
        if isinstance(forecast_summary, str) and QWEN_OUTAGE_MARKER in forecast_summary
    ]
    if degraded:
        logger.warning(
            "\n%s\n%d question(s) forecast on degraded research because SoCLaaS Qwen "
            "went down: %s\n%s",
            "#" * 100, len(degraded), degraded, "#" * 100,
        )

    errors = []
    for question_id_post_id, forecast_summary in zip(
        open_question_id_post_id, forecast_summaries
    ):
        question_id, post_id = question_id_post_id
        if isinstance(forecast_summary, Exception):
            formatted_traceback = "".join(
                traceback.format_exception(
                    type(forecast_summary),
                    forecast_summary,
                    forecast_summary.__traceback__,
                )
            ).rstrip()
            logger.error(
                "-----------------------------------------------\nPost %s Question %s:\n"
                "Error: %s %s\n"
                "Post API URL: %s/posts/%s/\n"
                "Traceback:\n%s",
                post_id,
                question_id,
                forecast_summary.__class__.__name__,
                forecast_summary,
                API_BASE_URL,
                post_id,
                formatted_traceback,
            )
            errors.append(forecast_summary)
        else:
            logger.info(forecast_summary)

    if errors:
        logger.error("\n%s\n%d question(s) FAILED:\n%s", "#" * 100, len(errors), "#" * 100)
        for err in errors:
            logger.error("  %s: %s", err.__class__.__name__, err)
        raise RuntimeError(f"{len(errors)} question(s) failed during forecasting")
