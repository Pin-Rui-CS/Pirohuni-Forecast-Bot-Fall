"""Who produced a forecast record: which run, which code, which models.

Every forecast.json carries this block so the forecast library can compare
results across code versions. None of it can be reconstructed afterwards --
once a run has finished, nothing records which commit or which models wrote
its artifacts -- so it is stamped at write time or not at all.

Bump SCHEMA_VERSION whenever the shape of forecast.json changes in a way a
reader must branch on (a renamed or restructured field). Adding a field does
not need a bump. Records written before this module existed have no
``schema_version`` and are treated as version 1 by the publisher.
"""

from __future__ import annotations

import os
import socket
import subprocess
from functools import lru_cache
from typing import Any

import llm_provider

# 1: legacy records, no provenance block.
# 2: adds provenance (run_id, code_sha, workflow, routing, CLI context),
#    `abstained`, and structured `llm_calls` alongside `usage_yaml_table`.
SCHEMA_VERSION = 2

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Set by forecasting_bot.py. Workflows that call orchestrator.forecast_questions
# directly never set it, so every key must tolerate being absent.
_cli_context: dict[str, Any] = {}


def set_cli_context(**context: Any) -> None:
    """Record what the CLI was asked to do (mode, tournaments, run count)."""
    _cli_context.update(context)


@lru_cache(maxsize=1)
def _local_git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=_REPO_ROOT,
        )
    except Exception:  # noqa: BLE001 - provenance must never fail a run
        return None
    sha = out.stdout.strip()
    return sha or None


def run_id(run_timestamp: str) -> str:
    """Stable identity for one execution of the bot.

    In Actions this is the run id plus attempt, so a re-run of a workflow is
    recorded as the separate execution it is. Locally there is no run id, so
    the timestamp and hostname stand in.
    """
    gh_run = os.getenv("GITHUB_RUN_ID")
    if gh_run:
        return f"gh-{gh_run}-{os.getenv('GITHUB_RUN_ATTEMPT') or '1'}"
    return f"local-{run_timestamp}-{socket.gethostname()}"


def _routing() -> dict[str, Any]:
    try:
        profile = llm_provider.active_profile()
        tier1 = llm_provider.route_for(llm_provider.TIER1_ROLE)
        tier2 = llm_provider.route_for(llm_provider.TIER2_ROLE)
        return {
            "mode": llm_provider.LLM_PROVIDER,
            "tier1_model": tier1.model_id,
            "tier1_endpoint": tier1.endpoint.label,
            "tier2_model": tier2.model_id,
            "tier2_endpoint": tier2.endpoint.label,
            "forecaster_pool": [
                llm_provider.route_for(name).model_id for name in profile.forecaster_pool
            ],
            "heterogeneous_model": llm_provider.route_for(profile.heterogeneous_model).model_id,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_provenance(run_timestamp: str) -> dict[str, Any]:
    """The provenance block written into every forecast.json."""
    server = os.getenv("GITHUB_SERVER_URL")
    repo = os.getenv("GITHUB_REPOSITORY")
    gh_run = os.getenv("GITHUB_RUN_ID")
    return {
        "run_id": run_id(run_timestamp),
        "code_sha": os.getenv("GITHUB_SHA") or _local_git_sha(),
        "workflow": os.getenv("GITHUB_WORKFLOW") or "local",
        "event": os.getenv("GITHUB_EVENT_NAME") or "local",
        "run_url": f"{server}/{repo}/actions/runs/{gh_run}" if server and repo and gh_run else None,
        "routing": _routing(),
        "cli": dict(_cli_context),
    }
