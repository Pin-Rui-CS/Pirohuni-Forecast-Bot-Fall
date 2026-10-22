"""Opt-in effort ladder, with model fallback, for calls routed to SoCLaaS.

(The module name predates DeepSeek; it serves every SoCLaaS model.) Each
model's rungs, sampler and reply names come from soclaas_models; the models
are tried in SOCLAAS_MODEL_CHAIN order (DeepSeek, then Qwen by default).

Enabled by QWEN_LADDER=1. When on, llm_provider hands every SoCLaaS route a
ladder client instead of the plain OpenAI client, so the four places that send
requests (call_llm, sync_chat, the compiler brief, the resolution scraper) all
get the same policy without changing a line at the call site:

  1. One streamed request at the label's starting effort (the model's deepest
     rung by default: DeepSeek high, Qwen XHigh).
  2. If it times out, loses the stream, finishes without [DONE]/stop, returns
     nothing, or the answer is unusable: one more request at the next rung
     (DeepSeek low, Qwen Medium), built from the same original kwargs -- never
     from the failed partial answer.
  3. HTTP 408/5xx, a refused connection, or a 429 that is the gateway's busy
     queue rather than a spent quota (``is_overload_429``) means the model is
     unavailable, which less thinking does not cure. The same rung is retried once after a
     backoff (UNAVAILABLE_BACKOFF_SECONDS), applied to the shared endpoint gate
     so every other SoCLaaS call pauses too. If the retry fails as well, the
     next model in the chain is tried; when the LAST one is unavailable too,
     SoCLaaS is down for the rest of the question (see OutageTally):
     QwenUnavailable is raised, and every later SoCLaaS call in that question
     fails at once. Research carries on with whatever the other providers got.
  4. Auth, access and quota refusals (and an HTTP 400/404 for a model the
     gateway rejects) end that model's ladder; less thinking cures none of
     them. Every stream holds one of the endpoint's ``max_in_flight`` slots
     (llm_provider.RateGate) while it runs.
  5. When one model's ladder fails, the same request goes to the next model in
     SOCLAAS_MODEL_CHAIN. When the last fails, raise QwenLadderFailed. It is
     not an OpenAI exception, so llm_client's three-attempt retry loop does not
     turn one ladder into three. There is no paid fallback here.

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

from bot.llm import provider as llm_provider
from bot.llm import soclaas_models

logger = logging.getLogger(__name__)

# Each model's ladder, sampler and reply names come from soclaas_models. These
# two are Qwen's, kept for older eval scripts that import them.
EFFORT_LADDER = soclaas_models.SOCLAAS_MODELS["qwen3.8:27b"].efforts
SAMPLER = dict(soclaas_models.SOCLAAS_MODELS["qwen3.8:27b"].sampler)
# max_tokens caps reasoning and answer together; XHigh needs the room.
MAX_TOKENS = 65536
RETRY_STATUS = frozenset({408, 500, 502, 503, 504})
# The gateway will not serve this model with these settings: try the next one.
MODEL_REJECTED_STATUS = frozenset({400, 404, 422})
# Wait before the one retry after SoCLaaS reports itself unavailable. On
# 2026-10-01 (Q46024) every Qwen call got HTTP 503 for the whole research
# window and each step spent both rungs within ~2 s; a lower effort never
# helped, a pause might.
UNAVAILABLE_BACKOFF_SECONDS: tuple[float, ...] = (30.0,)
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


class ModelRejected(StreamRefused):
    """The gateway would not serve THIS model (gone, renamed, or refusing these
    settings). The next model in the chain may still work."""


class AttemptFailed(RuntimeError):
    """This rung failed in a way the next rung might not."""


class Unavailable(AttemptFailed):
    """SoCLaaS answered 408/5xx or refused the connection: wait, don't step down."""


class QwenLadderFailed(RuntimeError):
    """Every rung failed, or a refusal stopped the ladder."""


class QwenUnavailable(QwenLadderFailed):
    """SoCLaaS was unavailable, after its retry or already down for this question."""


class QwenRefused(QwenLadderFailed):
    """An access or quota refusal. Every chain model shares the key and the
    allowance, so this ends the call without trying the next model."""


@dataclass
class OutageTally:
    """Per-question record of SoCLaaS going down.

    The orchestrator installs one per question (track_outages). The first
    ladder whose retry also fails marks SoCLaaS down; every later Qwen call in
    that question then fails at once instead of waiting out its own retry, and
    the orchestrator flags the forecast as built on degraded research.
    """
    unavailable_ladders: int = 0
    labels: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def down(self) -> bool:
        return self.unavailable_ladders > 0


_outage_tally: ContextVar[OutageTally | None] = ContextVar("qwen_outage_tally", default=None)


def track_outages() -> OutageTally:
    """Start a record for the current task (and the tasks/threads it spawns)."""
    tally = OutageTally()
    _outage_tally.set(tally)
    return tally


def _raise_if_down(label: str) -> None:
    tally = _outage_tally.get()
    if tally is not None and tally.down:
        tally.skipped.append(label)
        raise QwenUnavailable(f"{label}: skipped -- SoCLaaS is down for this question")


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


def reply_matches(requested: str, replied: str) -> bool:
    """True when ``replied`` is a name the gateway may use for ``requested``.

    Anything else -- the gateway silently serving a different model -- is still
    refused. DeepSeek answers "x-test-2" requests as "deepseek-v4.1-flash-1017",
    which its registry entry lists.
    """
    if replied == requested:
        return True
    model = soclaas_models.lookup(requested)
    return bool(model and model.accepts_reply(replied))


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
        if not reply_matches(expected_model, item["model"]):
            raise ModelRejected("Unexpected response model")
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
    "compiler/brief-audit": "## Key Evidence",
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
# "light" is each model's lightest rung (Qwen medium, DeepSeek low).
# QWEN_START_EFFORT still overrides.
# A key ending in "/" covers every label under it ("diagnostics/plan-items").
DEFAULT_START_EFFORT: dict[str, str] = {"apiagent": soclaas_models.LIGHT,
                                        "forecast-comment": soclaas_models.LIGHT,
                                        "diagnostics/": soclaas_models.LIGHT}


def start_effort(label: str) -> str:
    """Where a label's ladder starts: "deep", "light" or an effort name.

    QWEN_START_EFFORT="serp-scrape-extract=light" starts that label on its
    model's lightest rung, which leaves it a single attempt. "medium" (the
    value before per-model ladders) still works and means "light" for a model
    without a medium rung.
    """
    start = DEFAULT_START_EFFORT.get(label) or next(
        (effort for prefix, effort in DEFAULT_START_EFFORT.items()
         if prefix.endswith("/") and label.startswith(prefix)), soclaas_models.DEEP)
    for entry in os.getenv("QWEN_START_EFFORT", "").split(","):
        name, _, effort = entry.partition("=")
        if name.strip() and name.strip() == label and effort.strip():
            start = effort.strip().lower()
    return start


def efforts_for(label: str, model_id: str | None = None) -> tuple[str, ...]:
    """The rungs for one label on one model (default: the head of the chain)."""
    return soclaas_models.effort_ladder(model_id or soclaas_models.primary(), start_effort(label))


def set_attempt_log(path: Path | None) -> None:
    """Where each attempt is appended as one JSON line. None disables it."""
    global _attempt_log
    _attempt_log = Path(path) if path else None


# Per-question attempt log. Questions run concurrently, so a module global would
# interleave them; a ContextVar set inside the question's task follows that
# question's calls (child tasks inherit it). Written in addition to the global.
question_attempt_log: ContextVar[Path | None] = ContextVar("qwen_question_attempt_log",
                                                            default=None)


def _log_attempt(record: dict) -> None:
    logger.info(
        "[qwen-ladder] %s | %s | %s | %.1fs | answer=%d reasoning=%d chars%s",
        record["label"], record["effort"], record["outcome"], record["seconds"],
        record["answer_chars"], record["reasoning_chars"],
        f" | {record['error']}" if record.get("error") else "",
    )
    for target in {path for path in (question_attempt_log.get(), _attempt_log) if path}:
        with _log_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _payload(kwargs: dict[str, Any], effort: str, model_id: str | None = None) -> dict[str, Any]:
    """One attempt's request: the caller's messages/tools, this model and effort,
    the model's own sampler. Rebuilt from the caller's kwargs every attempt."""
    payload = {key: value for key, value in kwargs.items() if key not in _LADDER_KEYS}
    model_id = model_id or str(kwargs.get("model") or "")
    model = soclaas_models.lookup(model_id)
    caller_cap = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens") or 0
    payload.update(model=model_id, stream=True, stream_options={"include_usage": True},
                   reasoning_effort=effort, max_tokens=max(int(caller_cap), MAX_TOKENS),
                   **(model.sampler if model else SAMPLER))
    return payload


def is_overload_429(status: int, response: Any) -> bool:
    """A 429 that is SoCLaaS being busy, not our quota being spent.

    2026-10-05: ``429 {"error":"dispatch queue wait expired"}`` -- the request
    waited in the gateway's queue and was dropped. Waiting cures that; a spent
    daily/monthly budget it does not.
    """
    return status == 429 and not llm_provider.is_quota_exhaustion(
        SimpleNamespace(status_code=status, response=response))


def _refuse_or_retry(route: llm_provider.Route, status: int, response: Any) -> None:
    """Map an HTTP error status onto a rung failure or a hard stop."""
    llm_provider.note_failure(route, SimpleNamespace(status_code=status, response=response))
    if status in RETRY_STATUS or is_overload_429(status, response):
        raise Unavailable(f"HTTP {status}")
    if status in MODEL_REJECTED_STATUS:
        raise ModelRejected(f"SoCLaaS HTTP {status} for this model")
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


def _give_up_unavailable(label: str, failures: list[str], *, last_model: bool = True) -> QwenUnavailable:
    # Only the LAST model in the chain marks SoCLaaS down for the question: a
    # "testing" model can be unavailable while the rest of the gateway serves.
    if last_model:
        _note_unavailable_ladder(label)
    return QwenUnavailable(
        f"{label}: SoCLaaS unavailable, retry failed too"
        + ("; down for this question" if last_model else "") + " -- "
        + "; ".join(failures)
    )


def _attempt_quota(model_id: str, usage: dict | None) -> float:
    if not usage:
        return 0.0
    return soclaas_models.quota_microdollars(
        model_id, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


def _completion(state: StreamState, effort: str, attempts: list[tuple[str, dict | None]],
                model_id: str = ""):
    """The ChatCompletion handed back to the caller.

    ``attempts`` is (model, usage) for every attempt of this request, on every
    model tried, so the ledger books the quota a failed attempt spent too --
    each priced at the rate of the model that ran it (soclaas_quota_microdollars).
    """
    from openai.types.chat import ChatCompletion

    tool_calls = [
        {"id": call["id"] or f"call_{index}", "type": "function",
         "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
        for index, call in sorted(state.tool_calls.items()) if call["name"]
    ]
    known = [usage for _, usage in attempts if usage]
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
        "qwen_attempts": len(attempts),
        "soclaas_model": model_id,
        "soclaas_quota_microdollars": sum(_attempt_quota(m, u) for m, u in attempts),
    })


def _record(call_id, label, effort, outcome, start, state, error=None, model="") -> dict:
    return {
        "utc": datetime.now(timezone.utc).isoformat(), "call_id": call_id,
        "label": label, "model": model, "effort": effort,
        "outcome": outcome, "seconds": round(time.monotonic() - start, 3),
        "finish_reason": state.finish, "done_marker": state.done, "usage": state.usage,
        "answer_chars": len(state.answer), "reasoning_chars": len(state.reasoning),
        "error": error,
    }


def _chain_routes(route: llm_provider.Route) -> list[llm_provider.Route]:
    """The route asked for, then a route for each later model in the chain."""
    ids = soclaas_models.fallbacks_from(route.model_id)
    return [route] + [llm_provider.route_for_model(model_id) for model_id in ids[1:]]


def _fall_back(label: str, route: llm_provider.Route, nxt: llm_provider.Route,
               exc: QwenLadderFailed) -> None:
    logger.warning("[soclaas] %s: %s failed (%s); falling back to %s",
                   label, route.model_id, exc, nxt.model_id)


def _client_settings(route: llm_provider.Route, label: str = "") -> dict[str, Any]:
    return {
        "headers": {"Authorization": "Bearer " + llm_provider.api_key_for(route.endpoint)},
        "follow_redirects": False,
        "timeout": httpx.Timeout(connect=25, read=timeout_seconds(label), write=45, pool=30),
    }


async def run_async(route: llm_provider.Route, kwargs: dict[str, Any], *,
                    transport: httpx.AsyncBaseTransport | None = None):
    """One request through the ladder, falling back down SOCLAAS_MODEL_CHAIN.

    Each model gets its own full ladder. When it fails -- every rung failed,
    the gateway rejected the model (HTTP 400/404/422 or a reply naming another
    model), or it stayed unavailable after its retry -- the same request goes
    to the next model in the chain. An access or quota refusal ends the call
    (QwenRefused): the next model shares the allowance. When the last model
    fails, the call fails: there is no paid fallback.
    """
    label = current_label.get() or "unlabelled"
    _raise_if_down(label)
    call_id = uuid4().hex[:12]
    attempts: list[tuple[str, dict | None]] = []
    routes = _chain_routes(route)
    earlier: list[str] = []
    for index, model_route in enumerate(routes):
        last = index == len(routes) - 1
        try:
            return await _ladder_async(model_route, kwargs, label, call_id, attempts,
                                       transport=transport, last_model=last)
        except QwenRefused:
            raise
        except QwenLadderFailed as exc:
            if last:
                if earlier:
                    raise type(exc)(f"{exc} | earlier models: " + " | ".join(earlier)) from exc
                raise
            earlier.append(str(exc))
            _fall_back(label, model_route, routes[index + 1], exc)
            await llm_provider.gate_for(routes[index + 1]).wait_async()
    raise QwenLadderFailed(f"{label}: no SoCLaaS model to try")  # pragma: no cover


async def _ladder_async(route: llm_provider.Route, kwargs: dict[str, Any], label: str,
                        call_id: str, attempts: list[tuple[str, dict | None]], *,
                        transport: httpx.AsyncBaseTransport | None, last_model: bool):
    """Every rung on ONE model."""
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds(label)
    failures: list[str] = []
    try:
        efforts = efforts_for(label, route.model_id)
    except ValueError as exc:  # an unregistered model or an effort it lacks
        raise QwenLadderFailed(f"{label}: {exc}") from exc
    backoffs = list(UNAVAILABLE_BACKOFF_SECONDS)
    rung = 0
    async with httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(retries=0), **_client_settings(route, label),
    ) as client:
        while rung < len(efforts):
            effort = efforts[rung]
            state, start, outcome, error = StreamState(), time.monotonic(), "interrupted", None
            try:
                # The slot wait sits outside the deadline: queueing behind our
                # own streams must not eat this attempt's time.
                async with llm_provider.gate_for(route).in_flight_async():
                    start = time.monotonic()
                    async with asyncio.timeout(deadline):
                        payload = _payload(kwargs, effort, route.model_id)
                        async with client.stream("POST", url, json=payload) as response:
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
                failed = QwenLadderFailed if isinstance(exc, ModelRejected) else QwenRefused
                raise failed(f"{label}: {route.model_id}: {exc}") from exc
            finally:
                attempts.append((route.model_id, state.usage))
                _log_attempt(_record(call_id, label, effort, outcome, start, state, error,
                                     model=route.model_id))
            if outcome == "ok":
                return _completion(state, effort, attempts, route.model_id)
            failures.append(f"{effort} {outcome}: {error}")
            if outcome == "unavailable":
                if not backoffs:
                    raise _give_up_unavailable(label, failures, last_model=last_model)
                # Same rung again: the request was never served, so less
                # thinking would only waste the effort the label asked for.
                await _wait_unavailable_async(route, backoffs.pop(0))
                continue
            rung += 1
            if rung < len(efforts):
                # The caller gated the first request; a retry is another request.
                await llm_provider.gate_for(route).wait_async()
    raise QwenLadderFailed(f"{label}: every attempt on {route.model_id} failed -- " + "; ".join(failures))


def run_sync(route: llm_provider.Route, kwargs: dict[str, Any], *,
             transport: httpx.BaseTransport | None = None):
    """The same ladder and chain fallback for the synchronous prediction-market
    providers.

    httpx's read timeout bounds silence between chunks; the total deadline is
    checked between chunks, so a stream that keeps trickling cannot run on.
    """
    label = current_label.get() or "unlabelled"
    _raise_if_down(label)
    call_id = uuid4().hex[:12]
    attempts: list[tuple[str, dict | None]] = []
    routes = _chain_routes(route)
    earlier: list[str] = []
    for index, model_route in enumerate(routes):
        last = index == len(routes) - 1
        try:
            return _ladder_sync(model_route, kwargs, label, call_id, attempts,
                                transport=transport, last_model=last)
        except QwenRefused:
            raise
        except QwenLadderFailed as exc:
            if last:
                if earlier:
                    raise type(exc)(f"{exc} | earlier models: " + " | ".join(earlier)) from exc
                raise
            earlier.append(str(exc))
            _fall_back(label, model_route, routes[index + 1], exc)
            llm_provider.gate_for(routes[index + 1]).wait_sync()
    raise QwenLadderFailed(f"{label}: no SoCLaaS model to try")  # pragma: no cover


def _ladder_sync(route: llm_provider.Route, kwargs: dict[str, Any], label: str,
                 call_id: str, attempts: list[tuple[str, dict | None]], *,
                 transport: httpx.BaseTransport | None, last_model: bool):
    url = route.endpoint.base_url + "/chat/completions"
    deadline = timeout_seconds(label)
    failures: list[str] = []
    try:
        efforts = efforts_for(label, route.model_id)
    except ValueError as exc:
        raise QwenLadderFailed(f"{label}: {exc}") from exc
    backoffs = list(UNAVAILABLE_BACKOFF_SECONDS)
    rung = 0
    with httpx.Client(
        transport=transport or httpx.HTTPTransport(retries=0), **_client_settings(route, label),
    ) as client:
        while rung < len(efforts):
            effort = efforts[rung]
            state, start, outcome, error = StreamState(), time.monotonic(), "interrupted", None
            try:
                with llm_provider.gate_for(route).in_flight_sync():
                    start = time.monotonic()
                    payload = _payload(kwargs, effort, route.model_id)
                    with client.stream("POST", url, json=payload) as response:
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
                failed = QwenLadderFailed if isinstance(exc, ModelRejected) else QwenRefused
                raise failed(f"{label}: {route.model_id}: {exc}") from exc
            finally:
                attempts.append((route.model_id, state.usage))
                _log_attempt(_record(call_id, label, effort, outcome, start, state, error,
                                     model=route.model_id))
            if outcome == "ok":
                return _completion(state, effort, attempts, route.model_id)
            failures.append(f"{effort} {outcome}: {error}")
            if outcome == "unavailable":
                if not backoffs:
                    raise _give_up_unavailable(label, failures, last_model=last_model)
                _wait_unavailable_sync(route, backoffs.pop(0))
                continue
            rung += 1
            if rung < len(efforts):
                llm_provider.gate_for(route).wait_sync()
    raise QwenLadderFailed(f"{label}: every attempt on {route.model_id} failed -- " + "; ".join(failures))


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
