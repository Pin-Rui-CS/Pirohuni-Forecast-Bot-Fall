"""Opt-in XHigh-then-Medium ladder for research calls routed to SoCLaaS Qwen.

Enabled by QWEN_LADDER=1. When on, llm_provider hands every SoCLaaS route a
ladder client instead of the plain OpenAI client, so the four places that send
requests (call_llm, sync_chat, the compiler brief, the resolution scraper) all
get the same policy without changing a line at the call site:

  1. One streamed request at the label's starting effort (XHigh by default).
  2. If it times out, loses the stream, finishes without [DONE]/stop, returns
     nothing, or the answer is unusable: one more request at Medium, built from
     the same original kwargs -- never from the failed partial answer.
  3. HTTP 408/5xx or a refused connection means SoCLaaS itself is unavailable,
     which less thinking does not cure. The same rung is retried after a
     backoff (UNAVAILABLE_BACKOFF_SECONDS), and the backoff is applied to the
     shared endpoint gate so every other Qwen call pauses too instead of
     hammering an outage. When the backoffs run out, QwenUnavailable is raised
     and counted against the current question (see OutageTally).
  4. Auth, quota and 429 refusals stop immediately. Less thinking cures none
     of them.
  5. When every rung fails, raise QwenLadderFailed. It is not an OpenAI
     exception, so llm_client's three-attempt retry loop does not turn one
     ladder into three. There is no paid fallback here.

The caller gets an ordinary ChatCompletion whose usage is the SUM over every
attempt that reported usage, so the ledger books the quota a failed XHigh
spent too. A timed-out stream usually reports no usage; that quota stays
unknown rather than being counted as zero -- see the attempt log.

qwen_precompression shares the stream parsing below so both paths judge a
stream complete by the same rules.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx

import llm_provider

logger = logging.getLogger(__name__)

EFFORT_LADDER = ("xhigh", "medium")
# max_tokens caps reasoning and answer together; XHigh needs the room.
MAX_TOKENS = 65536
# The sampler the A1 screen and the precompression replay used.
SAMPLER = {"temperature": 1, "top_p": .95, "top_k": 20}
RETRY_STATUS = frozenset({408, 500, 502, 503, 504})
# Waits before re-sending after SoCLaaS reports itself unavailable. On
# 2026-10-01 (Q46024) every Qwen call got HTTP 503 for the whole research
# window; the old ladder spent both rungs within ~2 s and gave up, so one
# outage silently emptied the brief. ~100 s of patience rides out a blip;
# a longer outage is the orchestrator's job (wait_until_available + deferral).
UNAVAILABLE_BACKOFF_SECONDS: tuple[float, ...] = (10.0, 30.0, 60.0)
REFUSAL_CODES = frozenset({"401", "402", "403", "429"})
REFUSAL_TERMS = ("quota", "budget", "rate_limit", "rate limit", "unauthorized",
                 "forbidden", "authentication", "access denied")
# Rebuilt per attempt rather than inherited from the caller's kwargs.
_LADDER_KEYS = frozenset({"extra_body", "reasoning_effort", "stream", "stream_options",
                          "max_tokens", "max_completion_tokens", "temperature",
                          "top_p", "top_k", "seed"})

# Set by MonetaryCostManager.start_openrouter_call, which every call site runs
# immediately before create(). It is how the ladder knows which task it serves.
current_label: ContextVar[str] = ContextVar("qwen_ladder_label", default="")

_attempt_log: Path | None = None
_log_lock = threading.Lock()


class StreamRefused(RuntimeError):
    """Access, quota or model refusal. Not eligible for a lower rung."""


class AttemptFailed(RuntimeError):
    """This rung failed in a way the next rung might not."""


class Unavailable(AttemptFailed):
    """SoCLaaS answered 408/5xx or refused the connection: wait, don't step down."""


class QwenLadderFailed(RuntimeError):
    """Every rung failed, or a refusal stopped the ladder."""


class QwenUnavailable(QwenLadderFailed):
    """The ladder gave up because SoCLaaS stayed unavailable through every backoff."""


@dataclass
class OutageTally:
    """Per-question count of ladders that gave up because SoCLaaS was down.

    The orchestrator installs one per question (track_outages) and reads it
    after research to decide whether the brief is too hollow to submit. Once
    ``give_up_after`` ladders have given up, the outage is confirmed for this
    question and later ladders fail at once instead of each waiting out the
    backoffs: the question is going to be deferred anyway.
    """
    unavailable_ladders: int = 0
    labels: list[str] = field(default_factory=list)
    give_up_after: int = 0  # 0 = never short-circuit

    @property
    def confirmed(self) -> bool:
        return 0 < self.give_up_after <= self.unavailable_ladders


_outage_tally: ContextVar[OutageTally | None] = ContextVar("qwen_outage_tally", default=None)


def track_outages(give_up_after: int = 0) -> OutageTally:
    """Start counting for the current task (and the tasks/threads it spawns)."""
    tally = OutageTally(give_up_after=give_up_after)
    _outage_tally.set(tally)
    return tally


def _raise_if_outage_confirmed(label: str) -> None:
    tally = _outage_tally.get()
    if tally is not None and tally.confirmed:
        raise QwenUnavailable(
            f"{label}: skipped -- SoCLaaS outage already confirmed for this question "
            f"({tally.unavailable_ladders} steps gave up)"
        )


def _note_unavailable_ladder(label: str) -> None:
    tally = _outage_tally.get()
    if tally is not None:
        tally.unavailable_ladders += 1
        tally.labels.append(label)


@dataclass
class StreamState:
    answer: str = ""
    reasoning: str = ""
    finish: str | None = None
    done: bool = False
    usage: dict | None = None
    model: str | None = None
    # Streamed tool calls, keyed by the delta's index: the id and name arrive
    # in the first fragment, the JSON arguments in pieces after it.
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)


def consume(state: StreamState, raw: str, expected_model: str) -> None:
    """Fold one SSE ``data:`` payload into ``state``."""
    if raw == "[DONE]":
        state.done = True
        return
    item = json.loads(raw)
    if item.get("error"):
        err = item["error"]
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
        description = json.dumps(err).lower()
        if code in REFUSAL_CODES or any(term in description for term in REFUSAL_TERMS):
            raise StreamRefused("Stream reported an access/quota refusal")
        raise AttemptFailed("Stream returned an error")
    if item.get("model"):
        state.model = item["model"]
        if item["model"] != expected_model:
            raise StreamRefused("Unexpected response model")
    if item.get("usage"):
        state.usage = item["usage"]
    for choice in item.get("choices", []):
        if choice.get("index", 0) != 0:
            continue
        state.finish = choice.get("finish_reason") or state.finish
        delta = choice.get("delta") or {}
        state.answer += delta.get("content") or ""
        state.reasoning += delta.get("reasoning_content") or delta.get("reasoning") or ""
        for fragment in delta.get("tool_calls") or []:
            call = state.tool_calls.setdefault(
                int(fragment.get("index") or 0), {"id": "", "name": "", "arguments": ""})
            call["id"] = call["id"] or fragment.get("id") or ""
            function = fragment.get("function") or {}
            call["name"] = call["name"] or function.get("name") or ""
            call["arguments"] += function.get("arguments") or ""


def check_complete(state: StreamState) -> None:
    """Complete means [DONE] plus either an answer or at least one named tool call."""
    called = any(call["name"] for call in state.tool_calls.values())
    if not state.done or state.finish not in ("stop", "tool_calls"):
        raise AttemptFailed("Incomplete stream or unexpected finish")
    if not (state.answer.strip() or called):
        raise AttemptFailed("Empty answer and no tool call")


# A section an answer must contain to count as the requested output. The 45707
# A/B run's XHigh brief finished normally but replied "Understood. Send: 1. The
# forecast question ..." -- a complete stream that is not a brief at all.
REQUIRED_OUTPUT: dict[str, str] = {
    "compiler/research-brief": "## Key Evidence",
}


def check_required_output(label: str, state: StreamState) -> None:
    marker = REQUIRED_OUTPUT.get(label)
    if marker and marker not in state.answer:
        raise AttemptFailed(f"Answer lacks the required {marker!r} section")


def enabled() -> bool:
    return os.getenv("QWEN_LADDER", "").strip().lower() in {"1", "true", "yes", "on"}


def is_forecast_label(label: str) -> bool:
    """A forecast run ("numeric-forecast[qwen3.8:27b]") or the binary tiebreaker."""
    return "-forecast" in label or label == "binary-tiebreaker"


def timeout_seconds(label: str = "") -> float:
    """Per-attempt deadline. Forecast runs get longer than research calls.

    A forecast is one call whose thinking IS the output, and the raw-research
    run reads the whole corpus: on 46022 its 180k-char prompt timed out at
    XHigh after 600 s and the Medium retry came back empty, dropping the run.
    The question itself has a 3-hour limit, so 20 minutes per attempt fits.
    """
    if is_forecast_label(label):
        return float(os.getenv("QWEN_FORECAST_TIMEOUT_SECONDS", "1200"))
    return float(os.getenv("QWEN_LADDER_TIMEOUT_SECONDS", "600"))


# Labels whose calls are small decisions, not long reasoning: an API-agent step
# picks the next tool call, and XHigh would spend minutes on each of six steps.
# QWEN_START_EFFORT still overrides.
DEFAULT_START_EFFORT: dict[str, str] = {"apiagent": "medium", "forecast-comment": "medium"}


def efforts_for(label: str) -> tuple[str, ...]:
    """The rungs for one label: from its starting effort down the ladder.

    QWEN_START_EFFORT="serp-scrape-extract=medium" starts that label at Medium,
    which leaves it a single attempt -- there is no rung below Medium.
    """
    start = DEFAULT_START_EFFORT.get(label, EFFORT_LADDER[0])
    for entry in os.getenv("QWEN_START_EFFORT", "").split(","):
        name, _, effort = entry.partition("=")
        if name.strip() and name.strip() == label and effort.strip():
            start = effort.strip().lower()
    if start not in EFFORT_LADDER:
        raise ValueError(f"QWEN_START_EFFORT for {label!r} must be one of {EFFORT_LADDER}")
    return EFFORT_LADDER[EFFORT_LADDER.index(start):]


def set_attempt_log(path: Path | None) -> None:
    """Where each attempt is appended as one JSON line. None disables it."""
    global _attempt_log
    _attempt_log = Path(path) if path else None


def _log_attempt(record: dict) -> None:
    logger.info(
        "[qwen-ladder] %s | %s | %s | %.1fs | answer=%d reasoning=%d chars%s",
        record["label"], record["effort"], record["outcome"], record["seconds"],
        record["answer_chars"], record["reasoning_chars"],
        f" | {record['error']}" if record.get("error") else "",
    )
    if _attempt_log is None:
        return
    with _log_lock:
        _attempt_log.parent.mkdir(parents=True, exist_ok=True)
        with _attempt_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _payload(kwargs: dict[str, Any], effort: str) -> dict[str, Any]:
    payload = {key: value for key, value in kwargs.items() if key not in _LADDER_KEYS}
    caller_cap = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens") or 0
    payload.update(stream=True, stream_options={"include_usage": True},
                   reasoning_effort=effort, max_tokens=max(int(caller_cap), MAX_TOKENS),
                   **SAMPLER)
    return payload


def _refuse_or_retry(route: llm_provider.Route, status: int, response: Any) -> None:
    """Map an HTTP error status onto a rung failure or a hard stop."""
    llm_provider.note_failure(route, SimpleNamespace(status_code=status, response=response))
    if status in RETRY_STATUS:
        raise Unavailable(f"HTTP {status}")
    raise StreamRefused(f"SoCLaaS HTTP {status}; no lower-effort retry")


def _unavailable_backoff(route: llm_provider.Route, delay: float) -> bool:
    """Push the shared gate back by ``delay``. False when the route is ungated."""
    gate = llm_provider.gate_for(route)
    if not gate.enabled:
        return False
    gate.penalise(delay, reason="SoCLaaS unavailable")
    return True


async def _wait_unavailable_async(route: llm_provider.Route, delay: float) -> None:
    if _unavailable_backoff(route, delay):
        await llm_provider.gate_for(route).wait_async()
    else:
        await asyncio.sleep(delay)


def _wait_unavailable_sync(route: llm_provider.Route, delay: float) -> None:
    if _unavailable_backoff(route, delay):
        llm_provider.gate_for(route).wait_sync()
    else:
        time.sleep(delay)


def _give_up_unavailable(label: str, failures: list[str]) -> QwenUnavailable:
    _note_unavailable_ladder(label)
    return QwenUnavailable(
        f"{label}: SoCLaaS unavailable through {len(UNAVAILABLE_BACKOFF_SECONDS)} backoffs -- "
        + "; ".join(failures)
    )


def _completion(state: StreamState, effort: str, usages: list[dict | None]):
    from openai.types.chat import ChatCompletion

    tool_calls = [
        {"id": call["id"] or f"call_{index}", "type": "function",
         "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
        for index, call in sorted(state.tool_calls.items()) if call["name"]
    ]
    known = [usage for usage in usages if usage]
    usage = None
    if known:
        prompt = sum(int(u.get("prompt_tokens") or 0) for u in known)
        completion = sum(int(u.get("completion_tokens") or 0) for u in known)
        usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                 "total_tokens": prompt + completion}
    return ChatCompletion.model_validate({
        "id": "qwen-ladder-" + uuid4().hex[:12],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": state.model or "",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "message": {"role": "assistant", "content": state.answer,
                        "reasoning": state.reasoning,
                        **({"tool_calls": tool_calls} if tool_calls else {})},
        }],
        "usage": usage,
        "qwen_effort": effort,
        "qwen_attempts": len(usages),
    })


def _record(call_id, label, effort, outcome, start, state, error=None) -> dict:
    return {
        "utc": datetime.now(timezone.utc).isoformat(), "call_id": call_id,
        "label": label, "effort": effort,
        "outcome": outcome, "seconds": round(time.monotonic() - start, 3),
        "finish_reason": state.finish, "done_marker": state.done, "usage": state.usage,
        "answer_chars": len(state.answer), "reasoning_chars": len(state.reasoning),
        "error": error,
    }


def _client_settings(route: llm_provider.Route, label: str = "") -> dict[str, Any]:
    return {
        "headers": {"Authorization": "Bearer " + llm_provider.api_key_for(route.endpoint)},
        "follow_redirects": False,
        "timeout": httpx.Timeout(connect=25, read=timeout_seconds(label), write=45, pool=30),
    }


async def run_async(route: llm_provider.Route, kwargs: dict[str, Any], *,
                    transport: httpx.AsyncBaseTransport | None = None):
    label = current_label.get() or "unlabelled"
    _raise_if_outage_confirmed(label)
    call_id = uuid4().hex[:12]
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds(label)
    usages: list[dict | None] = []
    failures: list[str] = []
    efforts = efforts_for(label)
    backoffs = list(UNAVAILABLE_BACKOFF_SECONDS)
    rung = 0
    async with httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(retries=0), **_client_settings(route, label),
    ) as client:
        while rung < len(efforts):
            effort = efforts[rung]
            state, start, outcome, error = StreamState(), time.monotonic(), "interrupted", None
            try:
                async with asyncio.timeout(deadline):
                    async with client.stream("POST", url, json=_payload(kwargs, effort)) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            _refuse_or_retry(route, response.status_code, response)
                        async for line in response.aiter_lines():
                            raw = line[5:].strip() if line.startswith("data:") else ""
                            if raw:
                                consume(state, raw, route.model_id)
                            if state.done:
                                break
                check_complete(state)
                check_required_output(label, state)
                outcome = "ok"
            except TimeoutError:
                outcome, error = "timeout", f"no complete answer within {deadline:.0f}s"
            except (Unavailable, httpx.ConnectError, httpx.ConnectTimeout) as exc:
                outcome, error = "unavailable", f"{type(exc).__name__}: {exc}"
            except (httpx.TransportError, json.JSONDecodeError, AttemptFailed) as exc:
                outcome, error = "failed", f"{type(exc).__name__}: {exc}"
            except StreamRefused as exc:
                outcome, error = "refused", str(exc)
                raise QwenLadderFailed(f"{label}: {exc}") from exc
            finally:
                usages.append(state.usage)
                _log_attempt(_record(call_id, label, effort, outcome, start, state, error))
            if outcome == "ok":
                return _completion(state, effort, usages)
            failures.append(f"{effort} {outcome}: {error}")
            if outcome == "unavailable":
                if not backoffs:
                    raise _give_up_unavailable(label, failures)
                # Same rung again: the request was never served, so less
                # thinking would only waste the effort the label asked for.
                await _wait_unavailable_async(route, backoffs.pop(0))
                continue
            rung += 1
            if rung < len(efforts):
                # The caller gated the first request; a retry is another request.
                await llm_provider.gate_for(route).wait_async()
    raise QwenLadderFailed(f"{label}: every Qwen attempt failed -- " + "; ".join(failures))


def run_sync(route: llm_provider.Route, kwargs: dict[str, Any], *,
             transport: httpx.BaseTransport | None = None):
    """The same ladder for the synchronous prediction-market providers.

    httpx's read timeout bounds silence between chunks; the total deadline is
    checked between chunks, so a stream that keeps trickling cannot run on.
    """
    label = current_label.get() or "unlabelled"
    _raise_if_outage_confirmed(label)
    call_id = uuid4().hex[:12]
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds(label)
    usages: list[dict | None] = []
    failures: list[str] = []
    efforts = efforts_for(label)
    backoffs = list(UNAVAILABLE_BACKOFF_SECONDS)
    rung = 0
    with httpx.Client(
        transport=transport or httpx.HTTPTransport(retries=0), **_client_settings(route, label),
    ) as client:
        while rung < len(efforts):
            effort = efforts[rung]
            state, start, outcome, error = StreamState(), time.monotonic(), "interrupted", None
            try:
                with client.stream("POST", url, json=_payload(kwargs, effort)) as response:
                    if response.status_code >= 400:
                        response.read()
                        _refuse_or_retry(route, response.status_code, response)
                    for line in response.iter_lines():
                        if time.monotonic() - start > deadline:
                            raise TimeoutError
                        raw = line[5:].strip() if line.startswith("data:") else ""
                        if raw:
                            consume(state, raw, route.model_id)
                        if state.done:
                            break
                check_complete(state)
                check_required_output(label, state)
                outcome = "ok"
            except httpx.ConnectTimeout as exc:
                outcome, error = "unavailable", f"{type(exc).__name__}: {exc}"
            except (TimeoutError, httpx.TimeoutException):
                outcome, error = "timeout", f"no complete answer within {deadline:.0f}s"
            except (Unavailable, httpx.ConnectError) as exc:
                outcome, error = "unavailable", f"{type(exc).__name__}: {exc}"
            except (httpx.TransportError, json.JSONDecodeError, AttemptFailed) as exc:
                outcome, error = "failed", f"{type(exc).__name__}: {exc}"
            except StreamRefused as exc:
                outcome, error = "refused", str(exc)
                raise QwenLadderFailed(f"{label}: {exc}") from exc
            finally:
                usages.append(state.usage)
                _log_attempt(_record(call_id, label, effort, outcome, start, state, error))
            if outcome == "ok":
                return _completion(state, effort, usages)
            failures.append(f"{effort} {outcome}: {error}")
            if outcome == "unavailable":
                if not backoffs:
                    raise _give_up_unavailable(label, failures)
                _wait_unavailable_sync(route, backoffs.pop(0))
                continue
            rung += 1
            if rung < len(efforts):
                llm_provider.gate_for(route).wait_sync()
    raise QwenLadderFailed(f"{label}: every Qwen attempt failed -- " + "; ".join(failures))


# --- Is SoCLaaS up at all? ---------------------------------------------------
# Asked once per question before research (Q46024: SoCLaaS answered 503 for the
# whole research window and a one-page brief was submitted). Availability only:
# a 400 still means the server is answering.

_availability_lock: asyncio.Lock | None = None
_last_up_at = 0.0
_confirmed_down = False
# A probe that succeeded this recently is trusted without another request.
_FRESH_SECONDS = 120.0


def research_uses_soclaas() -> bool:
    """True when the ladder is on and research call sites are routed to SoCLaaS Qwen."""
    if not enabled():
        return False
    route = llm_provider.route_for_model(llm_provider.QWEN_RESEARCH_MODEL)
    if route.endpoint.name != llm_provider.ENDPOINT_SOCLAAS.name:
        return False
    return any(llm_provider.label_routes_to_qwen(label)
               for label in ("compiler/research-brief", "artifact-check", "google-query-generation"))


async def probe(*, transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, str]:
    """One tiny streamed request: (up, detail)."""
    route = llm_provider.route_for_model(llm_provider.QWEN_RESEARCH_MODEL)
    payload = {"model": route.model_id, "stream": True, "max_tokens": 256,
               "reasoning_effort": "medium",
               "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    try:
        await llm_provider.gate_for(route).wait_async()
        async with httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            headers={"Authorization": "Bearer " + llm_provider.api_key_for(route.endpoint)},
            timeout=httpx.Timeout(connect=15, read=90, write=15, pool=15),
        ) as client:
            async with client.stream("POST", route.endpoint.base_url + "/chat/completions",
                                     json=payload) as response:
                status = response.status_code
                if status >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    quota = llm_provider.is_quota_exhaustion(SimpleNamespace(
                        status_code=status, response=SimpleNamespace(text=body)))
                    if status in RETRY_STATUS or status in (401, 402, 403) or quota:
                        return False, f"HTTP {status} {body}".strip()
                    return True, f"HTTP {status}"
                async for line in response.aiter_lines():
                    raw = line[5:].strip() if line.startswith("data:") else ""
                    if raw == "[DONE]":
                        return True, f"HTTP {status}"
                    if raw:
                        error = json.loads(raw).get("error")
                        if error:
                            return False, f"HTTP {status} stream error: {json.dumps(error)[:200]}"
                        return True, f"HTTP {status}"
                return False, f"HTTP {status} but the stream closed without data"
    except (httpx.TransportError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def wait_until_available(*, backoffs: tuple[float, ...] | None = None,
                               transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, str]:
    """Probe, retrying on the ladder's own backoff schedule: (up, detail).

    Shared by concurrent questions: one probing sequence at a time, a recent
    success is reused, and once a full wait has ended down, later questions in
    the same run get a single probe instead of waiting the schedule again.
    """
    global _availability_lock, _last_up_at, _confirmed_down
    if _availability_lock is None:
        _availability_lock = asyncio.Lock()
    schedule = UNAVAILABLE_BACKOFF_SECONDS if backoffs is None else backoffs
    async with _availability_lock:
        if time.monotonic() - _last_up_at < _FRESH_SECONDS:
            return True, "recently probed"
        delays = () if _confirmed_down else schedule
        for attempt in range(len(delays) + 1):
            if attempt:
                await asyncio.sleep(delays[attempt - 1])
            up, detail = await probe(transport=transport)
            logger.info("[qwen-ladder] availability probe %d: %s (%s)",
                        attempt + 1, "up" if up else "DOWN", detail)
            if up:
                _last_up_at, _confirmed_down = time.monotonic(), False
                return True, detail
        _confirmed_down = True
        return False, detail


def reset_availability() -> None:
    """Forget cached availability (tests)."""
    global _availability_lock, _last_up_at, _confirmed_down
    _availability_lock, _last_up_at, _confirmed_down = None, 0.0, False


class _AsyncCompletions:
    def __init__(self, route: llm_provider.Route) -> None:
        self._route = route

    async def create(self, **kwargs: Any):
        return await run_async(self._route, kwargs)


class _SyncCompletions:
    def __init__(self, route: llm_provider.Route) -> None:
        self._route = route

    def create(self, **kwargs: Any):
        return run_sync(self._route, kwargs)


class AsyncLadderClient:
    """Stands in for AsyncOpenAI: exposes ``.chat.completions.create``."""

    def __init__(self, route: llm_provider.Route) -> None:
        self.chat = SimpleNamespace(completions=_AsyncCompletions(route))


class SyncLadderClient:
    """Stands in for OpenAI: exposes ``.chat.completions.create``."""

    def __init__(self, route: llm_provider.Route) -> None:
        self.chat = SimpleNamespace(completions=_SyncCompletions(route))
