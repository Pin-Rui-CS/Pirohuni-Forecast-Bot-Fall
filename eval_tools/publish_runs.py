"""Publish run artifacts to the forecast library (Supabase).

Walks a runs directory for forecast.json files and, for each question:

  1. uploads the question's files to storage under runs/<run_id>/<question_id>/
     -- forecast.json verbatim, the markdown files gzipped, trace/ as a tarball;
  2. upserts a `runs` row, a `forecasts` row and one `llm_calls` row per LLM call.

Files go up before rows, so a row never points at files that aren't there.
Everything is keyed on (run_id, question_id), so publishing the same folder
twice updates rather than duplicates. A question that fails is reported and
skipped; the rest still publish.

Records from before provenance existed (schema_version 1) are published too:
their per-call costs are recovered by parsing the rendered usage table by
column name, which works across every column layout that table has had.

Usage:
    poetry run python eval_tools/publish_runs.py [runs_root ...]
        [--run-id ID] [--code-sha SHA] [--workflow NAME] [--run-url URL]
        [--dry-run]

The --run-id/--code-sha/--workflow/--run-url overrides fill in provenance only
where a record has none, for backfilling artifacts written before it existed.

Without SUPABASE_URL and SUPABASE_SERVICE_KEY this prints a notice and exits 0,
so the workflow step is a no-op until the project exists.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import gzip
import io
import json
import math
import os
import re
import sys
import tarfile
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

from eval_tools.supabase_rest import SupabaseREST  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RUNS_ROOT = os.path.join(_REPO_ROOT, "docs", "runs")
BUCKET = "forecast-runs"

# Per-question files stored gzipped. Missing ones are skipped, not errors:
# an abstained or failed question may not have written all of them.
_TEXT_FILES = ("research.md", "runs.md", "audit.md", "evolution.md")

_LLM_CALL_COLUMNS = (
    "run_id", "question_id", "call_no", "task", "endpoint", "cost_source", "model",
    "input_tokens", "output_tokens", "native_input_tokens", "native_output_tokens",
    "reasoning_tokens", "cached_input_tokens", "cache_write_tokens",
    "cost_usd", "quota_microdollars", "duration_seconds",
)

# Rendered usage-table header -> llm_calls column. Matched by name, not
# position: the table gained "cache write" in one version and "quota ud" and
# "endpoint" in the next, and a name-based map reads all of them.
_TABLE_HEADER_TO_COLUMN = {
    "no.": "call_no",
    "name of task": "task",
    "input tokens": "input_tokens",
    "output tokens": "output_tokens",
    "reasoning tokens": "reasoning_tokens",
    "native in": "native_input_tokens",
    "native out": "native_output_tokens",
    "cached in": "cached_input_tokens",
    "cache write": "cache_write_tokens",
    "cost usd": "cost_usd",
    "quota ud": "quota_microdollars",
    "seconds": "duration_seconds",
    "endpoint": "endpoint",
    "model used": "model",
}
_TEXT_COLUMNS = {"task", "endpoint", "cost_source", "model"}


# ---------------------------------------------------------------------------
# Reading a record
# ---------------------------------------------------------------------------

def find_forecast_files(roots: list[str]) -> list[str]:
    files: list[str] = []
    for root in roots:
        files.extend(glob.glob(os.path.join(root, "**", "forecast.json"), recursive=True))
    return sorted(set(files))


def _sanitize(value: Any) -> Any:
    """Replace NaN/inf, which Python's json writes but Postgres jsonb rejects."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _run_at(run_timestamp: str | None) -> str | None:
    """docs/runs folder timestamp -> ISO timestamp. Actions runners use UTC."""
    if not run_timestamp:
        return None
    try:
        parsed = datetime.datetime.strptime(run_timestamp, "%Y-%m-%d_%H-%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc).isoformat()


@dataclass
class Overrides:
    run_id: str | None = None
    code_sha: str | None = None
    workflow: str | None = None
    run_url: str | None = None


@dataclass
class Prepared:
    """Everything one question publishes, built without touching the network."""

    run_id: str
    question_id: int
    blob_prefix: str
    run_row: dict
    forecast_row: dict
    llm_call_rows: list[dict]
    blobs: list[tuple[str, bytes, str]] = field(default_factory=list)


def _provenance(record: dict, overrides: Overrides) -> dict:
    prov = dict(record.get("provenance") or {})
    for key in ("run_id", "code_sha", "workflow", "run_url"):
        if not prov.get(key) and getattr(overrides, key):
            prov[key] = getattr(overrides, key)
    if not prov.get("run_id"):
        prov["run_id"] = f"local-{record.get('run_timestamp') or 'unknown'}"
    return prov


def _to_number(text: str) -> float | int | None:
    text = text.strip()
    if not text or text == "?":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() and "." not in text else number


def calls_from_rendered_table(usage_yaml_table: str) -> list[dict]:
    """Recover per-call rows from the markdown table in usage_yaml_table."""
    header: list[str] | None = None
    calls: list[dict] = []
    for line in (usage_yaml_table or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            if cells and cells[0] == "no.":
                header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue  # the |---|---| alignment row
        row: dict[str, Any] = {}
        for name, cell in zip(header, cells):
            column = _TABLE_HEADER_TO_COLUMN.get(name)
            if column is None:
                continue
            row[column] = cell if column in _TEXT_COLUMNS else _to_number(cell)
        if row.get("call_no") is not None:
            calls.append(row)
    return calls


def _calls_from_records(llm_calls: list[dict]) -> list[dict]:
    """Structured llm_calls (schema v2) -> llm_calls columns."""
    renamed = {"no": "call_no", "name_of_task": "task", "model_used": "model"}
    rows = []
    for call in llm_calls:
        row = {renamed.get(k, k): v for k, v in call.items()}
        rows.append(row)
    return rows


def _total_cost(record: dict, calls: list[dict]) -> float | None:
    if isinstance(record.get("total_cost_usd"), (int, float)):
        return float(record["total_cost_usd"])
    match = re.search(r"total_cost_usd:\s*([0-9.]+)", record.get("usage_yaml_table") or "")
    if match:
        return float(match.group(1))
    costs = [c.get("cost_usd") for c in calls if isinstance(c.get("cost_usd"), (int, float))]
    return float(sum(costs)) if costs else None


def _gzip(data: bytes) -> bytes:
    # mtime=0 keeps the output byte-identical across publishes of the same file.
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(data)
    return buffer.getvalue()


def _tarball(directory: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in sorted(os.listdir(directory)):
            tar.add(os.path.join(directory, name), arcname=name)
    return buffer.getvalue()


def prepare(path: str, overrides: Overrides) -> Prepared:
    with open(path, encoding="utf-8") as handle:
        raw = _sanitize(json.load(handle))

    question_dir = os.path.dirname(path)
    question_id = int(raw["question_id"])
    schema_version = int(raw.get("schema_version") or 1)
    prov = _provenance(raw, overrides)
    run_id = prov["run_id"]
    routing = prov.get("routing") or {}
    blob_prefix = f"runs/{run_id}/{question_id}"
    run_at = _run_at(raw.get("run_timestamp"))

    if schema_version >= 2 and isinstance(raw.get("llm_calls"), list):
        calls = _calls_from_records(raw["llm_calls"])
    else:
        calls = calls_from_rendered_table(raw.get("usage_yaml_table") or "")
    llm_call_rows = [
        {**{column: call.get(column) for column in _LLM_CALL_COLUMNS},
         "run_id": run_id, "question_id": question_id}
        for call in calls
    ]

    abstained = raw.get("abstained")
    if abstained is None:
        abstained = raw.get("final_forecast") is None

    run_row = {
        "run_id": run_id,
        "workflow": prov.get("workflow"),
        "event": prov.get("event"),
        "code_sha": prov.get("code_sha"),
        "run_url": prov.get("run_url"),
        "run_timestamp": raw.get("run_timestamp"),
        "started_at": run_at,
        "config": {"routing": routing, "cli": prov.get("cli") or {}},
    }
    forecast_row = {
        "run_id": run_id,
        "question_id": question_id,
        "post_id": raw.get("post_id"),
        "question_type": raw.get("question_type"),
        "title": raw.get("title"),
        "run_at": run_at,
        "workflow": prov.get("workflow"),
        "submitted": bool(raw.get("submitted")),
        "abstained": bool(abstained),
        "total_cost_usd": _total_cost(raw, calls),
        "estimated_tokens": raw.get("estimated_tokens"),
        "tier1_model": routing.get("tier1_model"),
        "tier2_model": routing.get("tier2_model"),
        "code_sha": prov.get("code_sha"),
        "schema_version": schema_version,
        "run_url": prov.get("run_url"),
        "blob_prefix": blob_prefix,
        "raw": raw,
    }

    with open(path, "rb") as handle:
        blobs = [(f"{blob_prefix}/forecast.json", handle.read(), "application/json")]
    for name in _TEXT_FILES:
        file_path = os.path.join(question_dir, name)
        if os.path.isfile(file_path):
            with open(file_path, "rb") as handle:
                blobs.append((f"{blob_prefix}/{name}.gz", _gzip(handle.read()), "application/gzip"))
    trace_dir = os.path.join(question_dir, "trace")
    if os.path.isdir(trace_dir) and os.listdir(trace_dir):
        blobs.append((f"{blob_prefix}/trace.tar.gz", _tarball(trace_dir), "application/gzip"))

    return Prepared(run_id, question_id, blob_prefix, run_row, forecast_row, llm_call_rows, blobs)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def publish(
    roots: list[str],
    client: SupabaseREST | None,
    overrides: Overrides | None = None,
    *,
    dry_run: bool = False,
    out=sys.stdout,
) -> tuple[int, int]:
    """Publish every forecast.json under ``roots``. Returns (published, failed)."""
    overrides = overrides or Overrides()
    files = find_forecast_files(roots)
    if not files:
        print(f"[publish] no forecast.json files under {roots}", file=out)
        return 0, 0

    published = failed = 0
    runs_done: set[str] = set()
    for path in files:
        try:
            item = prepare(path, overrides)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            failed += 1
            print(f"[publish] FAILED to read {path}: {type(exc).__name__}: {exc}", file=out)
            continue

        run_log = os.path.join(os.path.dirname(os.path.dirname(path)), "run.log")
        include_run_log = item.run_id not in runs_done and os.path.isfile(run_log)

        if dry_run:
            size = sum(len(data) for _, data, _ in item.blobs)
            print(
                f"[publish] dry-run {item.run_id} q{item.question_id}: "
                f"{len(item.blobs)} files ({size:,} bytes), {len(item.llm_call_rows)} llm_calls, "
                f"schema v{item.forecast_row['schema_version']}, "
                f"cost={item.forecast_row['total_cost_usd']}",
                file=out,
            )
            runs_done.add(item.run_id)
            published += 1
            continue

        try:
            for blob_path, data, content_type in item.blobs:
                client.upload(BUCKET, blob_path, data, content_type)
            if include_run_log:
                with open(run_log, "rb") as handle:
                    client.upload(BUCKET, f"runs/{item.run_id}/run.log.gz",
                                  _gzip(handle.read()), "application/gzip")
            if item.run_id not in runs_done:
                client.upsert("runs", [item.run_row], on_conflict="run_id")
            client.upsert("forecasts", [item.forecast_row], on_conflict="run_id,question_id")
            if item.llm_call_rows:
                client.upsert("llm_calls", item.llm_call_rows,
                              on_conflict="run_id,question_id,call_no")
        except Exception as exc:  # noqa: BLE001 - SupabaseError, network, disk
            failed += 1
            print(f"[publish] FAILED {item.run_id} q{item.question_id}: {exc}", file=out)
            continue

        runs_done.add(item.run_id)
        published += 1
        print(
            f"[publish] {item.run_id} q{item.question_id}: {len(item.blobs)} files, "
            f"{len(item.llm_call_rows)} llm_calls",
            file=out,
        )

    print(f"[publish] done: {published} published, {failed} failed", file=out)
    return published, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="*", default=[DEFAULT_RUNS_ROOT])
    parser.add_argument("--run-id")
    parser.add_argument("--code-sha")
    parser.add_argument("--workflow")
    parser.add_argument("--run-url")
    parser.add_argument("--dry-run", action="store_true",
                        help="build everything and report it, without contacting Supabase")
    args = parser.parse_args(argv)

    dotenv.load_dotenv(os.path.join(_REPO_ROOT, ".env"))
    overrides = Overrides(args.run_id, args.code_sha, args.workflow, args.run_url)

    if args.dry_run:
        _, failed = publish(args.roots, None, overrides, dry_run=True)
        return 1 if failed else 0

    client = SupabaseREST.from_env()
    if client is None:
        print("[publish] SUPABASE_URL / SUPABASE_SERVICE_KEY not set; skipping.")
        return 0
    with client:
        _, failed = publish(args.roots, client, overrides)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
