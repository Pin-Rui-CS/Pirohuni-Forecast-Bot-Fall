"""Log every external call made while a question is being traced.

``install()`` (once per process, from the orchestrator) wraps httpx's
``Client.send`` / ``AsyncClient.send``, so every provider that speaks HTTP
through httpx -- search, scraping, AskNews, and the OpenAI SDK behind SoCLaaS
and OpenRouter -- is captured with no per-provider code. Calls made outside
httpx (the Node API agent) are added with ``record()``.

Each call is one line in ``<question>/trace/calls.jsonl``: when, how long,
service, request (credentials masked), status code, bytes, error, the
provider lane and model-call label in effect, and an id. ``research_trace.emit``
claims the calls its asyncio task made since its last event, which ties each
call to the step that made it (diagnostics/LINEAGE.md, "calls").

Like research_trace: never raises, never changes a request or a response.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import threading
import time
from contextvars import ContextVar
from urllib.parse import urlsplit

from bot.trace import context

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INSTALLED = False
_COUNTERS: dict[str, int] = {}
_PENDING: dict[tuple[str, int], list[str]] = {}  # (trace dir, task id) -> unclaimed call ids
_LABEL: ContextVar[str] = ContextVar("call_log_label", default="")
_SECRET = re.compile(r"((?:api[_-]?key|apikey|key|token|access_token|secret|auth|signature|sig)=)[^&\s'\"<>]+", re.I)
_MAX_BODY_FOR_TARGET = 20_000


def set_label(label: str):
    """Name the model call about to be made (llm_client); returns a reset token."""
    return _LABEL.set(str(label or ""))


def reset_label(token) -> None:
    try:
        _LABEL.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1***", str(text or ""))


def _task_key() -> int:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return id(task) if task is not None else threading.get_ident()


def _context() -> tuple[str, dict]:
    trace_dir = context.ACTIVE.get()
    record = context.SCOPE.get()
    try:
        from bot.trace.source_ledger import current_context

        _, phase = current_context()
    except Exception:  # noqa: BLE001
        phase = None
    return trace_dir, {"lane": record["lane"] if record else None, "pass": phase}


def record(*, service: str, kind: str, request: str, status: int | None = None,
           duration_s: float | None = None, returned: str | int | None = None, error: str = "",
           started_at: str | None = None, target: str | None = None) -> str | None:
    """Add one call to the active question's log. Returns its id, or None."""
    try:
        trace_dir, ctx = _context()
        if not trace_dir:
            return None
        with _LOCK:
            _COUNTERS[trace_dir] = _COUNTERS.get(trace_dir, 0) + 1
            call_id = f"k{_COUNTERS[trace_dir]}"
            _PENDING.setdefault((trace_dir, _task_key()), []).append(call_id)
        row = {
            "id": call_id,
            "ts": started_at or datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(duration_s, 2) if duration_s is not None else None,
            "service": service, "kind": kind, "request": _redact(request)[:400],
            "status": status, "returned": returned, "error": _redact(error)[:300],
            "label": _LABEL.get() or None, "target": target, **ctx,
        }
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _LOCK, open(os.path.join(trace_dir, "calls.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return call_id
    except Exception as exc:  # noqa: BLE001 - logging must never fail a call
        logger.debug("call_log.record failed: %s", exc)
        return None


def claim() -> list[str]:
    """Ids of the calls this task made since its last claim (research_trace.emit)."""
    try:
        key = (context.ACTIVE.get(), _task_key())
        with _LOCK:
            return _PENDING.pop(key, [])
    except Exception:  # noqa: BLE001
        return []


def _kind(host: str) -> str:
    host = host.lower()
    if any(h in host for h in ("openrouter", "openai.com", "soclaas", "anthropic")):
        return "model"
    return "http"


def _target(request) -> str | None:
    """The page a scraping API was asked for, when the JSON body names one."""
    try:
        body = request.content
        if not body or len(body) > _MAX_BODY_FOR_TARGET:
            return None
        data = json.loads(body)
        url = data.get("url") if isinstance(data, dict) else None
        return str(url)[:400] if url else None
    except Exception:  # noqa: BLE001
        return None


def _log_http(request, response, started: float, started_at: str, error: str = "", streamed: bool = False):
    url = urlsplit(str(request.url))
    status = getattr(response, "status_code", None)
    size = None
    try:
        size = response.num_bytes_downloaded if response is not None else None
    except Exception:  # noqa: BLE001
        pass
    if streamed and response is not None and not getattr(response, "is_stream_consumed", True) and not error:
        error = "stream closed before it was fully read"
    record(service=url.hostname or "", kind=_kind(url.hostname or ""),
           request=f"{request.method} {url.path}" + (f"?{url.query}" if url.query else ""),
           status=status, duration_s=time.monotonic() - started, returned=size, error=error,
           started_at=started_at, target=_target(request))


def install() -> None:
    """Wrap httpx once per process. Safe to call repeatedly; never raises."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return
    async_send, sync_send = httpx.AsyncClient.send, httpx.Client.send

    async def logged_async_send(self, request, *args, **kwargs):
        started, started_at = time.monotonic(), datetime.datetime.now().isoformat(timespec="seconds")
        try:
            response = await async_send(self, request, *args, **kwargs)
        except BaseException as exc:
            _log_http(request, None, started, started_at, error=f"{type(exc).__name__}: {exc}")
            raise
        if kwargs.get("stream"):
            close = response.aclose

            async def logged_close():
                try:
                    await close()
                finally:
                    _log_http(request, response, started, started_at, streamed=True)
            response.aclose = logged_close
        else:
            _log_http(request, response, started, started_at)
        return response

    def logged_sync_send(self, request, *args, **kwargs):
        started, started_at = time.monotonic(), datetime.datetime.now().isoformat(timespec="seconds")
        try:
            response = sync_send(self, request, *args, **kwargs)
        except BaseException as exc:
            _log_http(request, None, started, started_at, error=f"{type(exc).__name__}: {exc}")
            raise
        if kwargs.get("stream"):
            close = response.close

            def logged_close():
                try:
                    close()
                finally:
                    _log_http(request, response, started, started_at, streamed=True)
            response.close = logged_close
        else:
            _log_http(request, response, started, started_at)
        return response

    httpx.AsyncClient.send = logged_async_send
    httpx.Client.send = logged_sync_send
    _install_requests()
    _INSTALLED = True


def _install_requests() -> None:
    """Some vendor SDKs (e.g. scraping clients) use requests rather than httpx."""
    try:
        import requests
    except Exception:  # noqa: BLE001
        return
    original = requests.Session.send

    def logged_send(self, request, **kwargs):
        started, started_at = time.monotonic(), datetime.datetime.now().isoformat(timespec="seconds")
        url = urlsplit(str(request.url))
        try:
            response = original(self, request, **kwargs)
        except BaseException as exc:
            record(service=url.hostname or "", kind=_kind(url.hostname or ""), request=f"{request.method} {url.path}",
                   duration_s=time.monotonic() - started, error=f"{type(exc).__name__}: {exc}", started_at=started_at)
            raise
        target = None
        try:
            body = request.body if isinstance(request.body, (bytes, str)) else None
            if body and len(body) <= _MAX_BODY_FOR_TARGET:
                data = json.loads(body)
                target = str(data.get("url"))[:400] if isinstance(data, dict) and data.get("url") else None
        except Exception:  # noqa: BLE001
            pass
        record(service=url.hostname or "", kind=_kind(url.hostname or ""),
               request=f"{request.method} {url.path}" + (f"?{url.query}" if url.query else ""),
               status=response.status_code, duration_s=time.monotonic() - started,
               returned=len(response.content) if not kwargs.get("stream") else None,
               started_at=started_at, target=target)
        return response

    requests.Session.send = logged_send
