"""Opt-in XHigh-then-Medium ladder for research calls routed to SoCLaaS Qwen.

Enabled by QWEN_LADDER=1. When on, llm_provider hands every SoCLaaS route a
ladder client instead of the plain OpenAI client, so the four places that send
requests (call_llm, sync_chat, the compiler brief, the resolution scraper) all
get the same policy without changing a line at the call site:

  1. One streamed request at the label's starting effort (XHigh by default).
  2. If it times out, loses the stream, finishes without [DONE]/stop, returns
     nothing, or gets HTTP 408/5xx: one more request at Medium, built from the
     same original kwargs -- never from the failed partial answer.
  3. Auth, quota and 429 refusals stop immediately. Less thinking cures none
     of them.
  4. When every rung fails, raise QwenLadderFailed. It is not an OpenAI
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
from dataclasses import dataclass
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


class QwenLadderFailed(RuntimeError):
    """Every rung failed, or a refusal stopped the ladder."""


@dataclass
class StreamState:
    answer: str = ""
    reasoning: str = ""
    finish: str | None = None
    done: bool = False
    usage: dict | None = None
    model: str | None = None


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


def check_complete(state: StreamState) -> None:
    if not state.done or state.finish != "stop" or not state.answer.strip():
        raise AttemptFailed("Incomplete stream, empty answer or non-stop finish")


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


def timeout_seconds() -> float:
    return float(os.getenv("QWEN_LADDER_TIMEOUT_SECONDS", "600"))


def efforts_for(label: str) -> tuple[str, ...]:
    """The rungs for one label: from its starting effort down the ladder.

    QWEN_START_EFFORT="serp-scrape-extract=medium" starts that label at Medium,
    which leaves it a single attempt -- there is no rung below Medium.
    """
    start = EFFORT_LADDER[0]
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
        raise AttemptFailed(f"HTTP {status}")
    raise StreamRefused(f"SoCLaaS HTTP {status}; no lower-effort retry")


def _completion(state: StreamState, effort: str, usages: list[dict | None]):
    from openai.types.chat import ChatCompletion

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
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": state.answer,
                        "reasoning": state.reasoning},
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


def _client_settings(route: llm_provider.Route) -> dict[str, Any]:
    return {
        "headers": {"Authorization": "Bearer " + llm_provider.api_key_for(route.endpoint)},
        "follow_redirects": False,
        "timeout": httpx.Timeout(connect=25, read=timeout_seconds(), write=45, pool=30),
    }


async def run_async(route: llm_provider.Route, kwargs: dict[str, Any], *,
                    transport: httpx.AsyncBaseTransport | None = None):
    label = current_label.get() or "unlabelled"
    call_id = uuid4().hex[:12]
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds()
    usages: list[dict | None] = []
    failures: list[str] = []
    async with httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(retries=0), **_client_settings(route),
    ) as client:
        for rung, effort in enumerate(efforts_for(label)):
            if rung:
                # The caller gated the first request; a retry is another request.
                await llm_provider.gate_for(route).wait_async()
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
    raise QwenLadderFailed(f"{label}: every Qwen attempt failed -- " + "; ".join(failures))


def run_sync(route: llm_provider.Route, kwargs: dict[str, Any], *,
             transport: httpx.BaseTransport | None = None):
    """The same ladder for the synchronous prediction-market providers.

    httpx's read timeout bounds silence between chunks; the total deadline is
    checked between chunks, so a stream that keeps trickling cannot run on.
    """
    label = current_label.get() or "unlabelled"
    call_id = uuid4().hex[:12]
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds()
    usages: list[dict | None] = []
    failures: list[str] = []
    with httpx.Client(
        transport=transport or httpx.HTTPTransport(retries=0), **_client_settings(route),
    ) as client:
        for rung, effort in enumerate(efforts_for(label)):
            if rung:
                llm_provider.gate_for(route).wait_sync()
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
            except (TimeoutError, httpx.TimeoutException):
                outcome, error = "timeout", f"no complete answer within {deadline:.0f}s"
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
    raise QwenLadderFailed(f"{label}: every Qwen attempt failed -- " + "; ".join(failures))


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
