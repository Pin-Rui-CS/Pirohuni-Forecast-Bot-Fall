"""Python access to apiagent-kit's 24 public data APIs.

apiagent-kit is TypeScript, so every call crosses a process boundary: this
module shells out to ``apiagent-kit/cli.ts`` under Node, writes one JSON
request on stdin and reads one JSON response from stdout. The child inherits
this process's environment, which ``dotenv.load_dotenv()`` has already
populated from the repo-root ``.env`` -- so ``FRED_API_KEY`` and friends reach
the adapters with no second config file.

WHY A SUBPROCESS PER CALL rather than a long-running sidecar: the bot makes a
handful of these per question, not hundreds, and a fresh process needs no
lifecycle, no health check, and no cleanup on a crashed run. Node starts in
~120 ms here, which is noise beside a 30 s scrape. Revisit if call volume
grows by an order of magnitude.

WHAT THIS IS FOR. Two things the scraping path does badly:

  * **Exact counts.** Several adapters filter a full published file locally
    and report the true matched total even when the rows are capped. Q44879
    ("how many CVEs will CISA add in August") spent $0.58 -- 27% of the run --
    buying that number from aggregators; `cisa_kev` states it outright.
  * **Clean historical series.** Seven adapters return dated `{date, value}`
    rows, which is what `series_reduce` wants and what `series_discovery`
    currently has to reverse-engineer out of JavaScript bundles.

FAILURE POLICY. Nothing here raises into the pipeline. Node missing, kit
missing, bad JSON, timeout, HTTP error -- all come back as a result dict with
``ok=False`` and a reason, matching how `research/` providers already degrade.
A forecasting run must never die because an optional data source was absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import dotenv

# Load the repo-root .env defensively, rather than relying on config.py having
# been imported first. The child process inherits os.environ, so without this
# an entry point that imports only this module -- an eval_tools script, a test,
# a one-off probe -- silently loses every credential-gated adapter. The failure
# is invisible: `fred` and `metaculus` simply report as gated off, which is
# indistinguishable from genuinely not having the keys. Idempotent, and does
# not override anything already in the environment.
dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logger = logging.getLogger(__name__)

KIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apiagent-kit")
CLI_PATH = os.path.join(KIT_DIR, "cli.ts")

# Node strips TypeScript types natively from 22.6; below that `cli.ts` will not
# even parse. Checked once and cached, because the answer cannot change within
# a run.
MIN_NODE_MAJOR = 22
MIN_NODE_MINOR = 6

DEFAULT_TIMEOUT_SECONDS = 60.0
# A single adapter response is small (<=25 rows), but CISA KEV and Cboe VIX
# read whole published files upstream. This guards against a pathological
# stdout, not against normal traffic.
_MAX_STDOUT_BYTES = 8 * 1024 * 1024

_availability: tuple[bool, str] | None = None


@dataclass
class AdapterResult:
    """One adapter call. ``ok`` is the only field that is always meaningful."""

    ok: bool
    api: str = ""
    error: str = ""
    tier: str = ""
    url: str = ""
    retrieved_at: str = ""
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    # Adapter-authored caveat. Frequently carries the load-bearing fact -- the
    # exact matched count, the period high/low, the staleness of the feed --
    # so it must travel with the rows rather than being dropped as prose.
    note: str = ""
    kind: str = ""          # params | shape | http | timeout | error
    retryable: bool = False
    params_help: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def has_rows(self) -> bool:
        return self.ok and bool(self.rows)


def _node_version_ok(raw: str) -> tuple[bool, str]:
    text = (raw or "").strip().lstrip("v")
    parts = text.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return False, f"could not parse node version {raw!r}"
    if (major, minor) < (MIN_NODE_MAJOR, MIN_NODE_MINOR):
        return False, (
            f"node {text} is too old; apiagent-kit needs >= "
            f"{MIN_NODE_MAJOR}.{MIN_NODE_MINOR} for native TypeScript stripping"
        )
    return True, text


def availability() -> tuple[bool, str]:
    """(usable, reason). Cheap, cached, and safe to call on every question."""
    global _availability
    if _availability is not None:
        return _availability

    node = shutil.which("node")
    if not node:
        _availability = (False, "node is not on PATH")
    elif not os.path.isfile(CLI_PATH):
        _availability = (False, f"apiagent-kit CLI not found at {CLI_PATH}")
    else:
        import subprocess

        try:
            out = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=15
            )
            ok, detail = _node_version_ok(out.stdout)
            _availability = (ok, detail if ok else detail)
        except Exception as exc:  # noqa: BLE001 - availability must never raise
            _availability = (False, f"node --version failed: {exc}")

    usable, reason = _availability
    logger.info("[apiagent] %s (%s)", "available" if usable else "unavailable", reason)
    return _availability


async def _run_cli(request: dict, timeout: float) -> dict:
    """One request/response round trip. Returns a payload dict, never raises."""
    usable, reason = availability()
    if not usable:
        return {"ok": False, "error": f"apiagent-kit unavailable: {reason}"}

    payload = json.dumps(request)
    try:
        proc = await asyncio.create_subprocess_exec(
            shutil.which("node") or "node",
            CLI_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=KIT_DIR,          # so the kit resolves its own imports
            env=os.environ.copy(),  # dotenv already populated this
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not start node: {exc}"}

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        # communicate() leaves the child running on timeout.
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "error": f"apiagent-kit timed out after {timeout:.0f}s",
            "kind": "timeout",
            "retryable": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"apiagent-kit call failed: {exc}"}

    if stderr:
        # The CLI keeps stdout clean, so anything here is diagnostics.
        logger.debug("[apiagent] stderr: %s", stderr.decode("utf-8", "replace")[:2000])

    if len(stdout) > _MAX_STDOUT_BYTES:
        return {"ok": False, "error": f"apiagent-kit returned {len(stdout)} bytes; refusing"}

    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        tail = stderr.decode("utf-8", "replace")[-500:] if stderr else ""
        return {
            "ok": False,
            "error": f"apiagent-kit returned nothing (exit {proc.returncode}). {tail}".strip(),
        }
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"apiagent-kit returned non-JSON: {exc}: {text[:300]}"}


def _to_result(api: str, payload: dict) -> AdapterResult:
    if not payload.get("ok"):
        return AdapterResult(
            ok=False,
            api=payload.get("api") or api,
            error=str(payload.get("error") or "unknown error"),
            kind=str(payload.get("kind") or ""),
            retryable=bool(payload.get("retryable")),
            params_help=str(payload.get("paramsHelp") or ""),
            raw=payload,
        )
    rows = payload.get("rows")
    return AdapterResult(
        ok=True,
        api=payload.get("api") or api,
        tier=str(payload.get("tier") or ""),
        url=str(payload.get("url") or ""),
        retrieved_at=str(payload.get("retrievedAt") or ""),
        rows=rows if isinstance(rows, list) else [],
        row_count=int(payload.get("rowCount") or 0),
        truncated=bool(payload.get("truncated")),
        note=str(payload.get("note") or ""),
        raw=payload,
    )


async def call_api(
    api: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AdapterResult:
    """Call one adapter by id. See ``find_apis`` for discovering the id."""
    payload = await _run_cli(
        {"op": "call", "api": api, "params": params or {},
         "timeoutMs": int(timeout * 1000)},
        timeout + 10,   # let the child's own timeout fire first
    )
    result = _to_result(api, payload)
    if result.ok:
        logger.info(
            "[apiagent] %s -> tier %s, %d rows%s | %s",
            result.api, result.tier, result.row_count,
            " (truncated)" if result.truncated else "", result.url,
        )
    else:
        logger.info("[apiagent] %s failed: %s", api, result.error)
    return result


async def find_apis(
    need: str, *, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Deterministic shortlist of adapters that could answer ``need``.

    No model and no network -- a keyword/stem match over hand-written prose.
    Free and offline, so it is safe to run on every question. Returns [] when
    nothing matches or the bridge is unavailable.
    """
    payload = await _run_cli({"op": "find", "need": need}, timeout)
    if not payload.get("ok"):
        logger.info("[apiagent] find failed: %s", payload.get("error"))
        return []
    candidates = payload.get("candidates")
    return candidates if isinstance(candidates, list) else []


async def list_adapters(*, timeout: float = 30.0) -> dict[str, Any]:
    """Every reachable adapter, plus the ids gated off by a missing key."""
    return await _run_cli({"op": "list"}, timeout)
