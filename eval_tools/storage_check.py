"""How full the Supabase project is: file storage measured exactly, the
database estimated. Fails (exit 1) at or past WARN_SHARE of either limit, so
the scheduled workflow's failure email is the warning.

    poetry run python eval_tools/storage_check.py [--warn-share 0.8]

Limits default to the free plan's (500 MB database, 1 GB file storage, per
supabase.com/pricing, checked 2026-11-27); set DATABASE_LIMIT_MB and
STORAGE_LIMIT_MB for another plan.

The database figure is an ESTIMATE: row counts are exact, row size is the mean
JSON size of a sample of rows. The REST API cannot report on-disk size, which
is larger (indexes, row overhead); the Supabase dashboard shows the real one.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

from eval_tools.publish_runs import BUCKET  # noqa: E402
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402

# Base tables only: the other relations the API lists are views over these.
TABLES = ("forecasts", "llm_calls", "runs", "outcomes", "scores", "run_diagnostics", "run_lineage")
SAMPLE_ROWS = 50
MB = 1_000_000


def file_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return "steps/*" if "/steps/" in path else name


def storage_usage(client: SupabaseREST) -> tuple[int, Counter, Counter]:
    """(total bytes, bytes per file kind, files per file kind)."""
    size, count = Counter(), Counter()
    for path, nbytes in client.walk_files(BUCKET):
        size[file_kind(path)] += nbytes
        count[file_kind(path)] += 1
    return sum(size.values()), size, count


def table_estimate(client: SupabaseREST, table: str) -> tuple[int, float]:
    """(exact row count, estimated bytes)."""
    response = client._request(
        "GET", f"/rest/v1/{table}", f"count {table}",
        params={"select": "*", "limit": str(SAMPLE_ROWS)},
        headers={"Prefer": "count=exact", "Range": f"0-{SAMPLE_ROWS - 1}"})
    rows = int(response.headers.get("content-range", "*/0").split("/")[-1] or 0)
    sample = response.json() or []
    mean = sum(len(json.dumps(r)) for r in sample) / len(sample) if sample else 0.0
    return rows, mean * rows


def report(client: SupabaseREST, warn_share: float) -> tuple[str, bool]:
    storage_limit = float(os.getenv("STORAGE_LIMIT_MB") or 1000) * MB
    database_limit = float(os.getenv("DATABASE_LIMIT_MB") or 500) * MB
    total, size, count = storage_usage(client)
    tables = {t: table_estimate(client, t) for t in TABLES}
    db_total = sum(b for _, b in tables.values())
    stored_runs = count.get("forecast.json", 0)
    per_run = total / stored_runs if stored_runs else 0.0
    runs_left = int((storage_limit - total) / per_run) if per_run else None

    lines = [
        "## Supabase storage check",
        "",
        "| | Used | Limit | Share |",
        "| --- | ---: | ---: | ---: |",
        f"| File storage (`{BUCKET}`) | {total / MB:,.1f} MB | {storage_limit / MB:,.0f} MB | {total / storage_limit:.0%} |",
        f"| Database (estimate) | {db_total / MB:,.1f} MB | {database_limit / MB:,.0f} MB | {db_total / database_limit:.0%} |",
        "",
        f"{stored_runs} stored question runs, {per_run / MB:.2f} MB each on average"
        + (f"; about {runs_left:,} more fit in file storage." if runs_left is not None else "."),
        "",
        "### File storage by kind",
        "",
        "| File | Files | MB |",
        "| --- | ---: | ---: |",
        *(f"| {kind} | {count[kind]:,} | {nbytes / MB:,.1f} |" for kind, nbytes in size.most_common()),
        "",
        "### Database tables (rows exact, size estimated)",
        "",
        "| Table | Rows | MB |",
        "| --- | ---: | ---: |",
        *(f"| {t} | {rows:,} | {nbytes / MB:,.1f} |" for t, (rows, nbytes) in tables.items()),
    ]
    over = total >= warn_share * storage_limit or db_total >= warn_share * database_limit
    if over:
        lines += ["", f"**WARNING: at or past {warn_share:.0%} of a limit.** Time to act (see the storage options)."]
    return "\n".join(lines) + "\n", over


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warn-share", type=float, default=0.8)
    args = parser.parse_args(argv)
    dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    client = SupabaseREST.from_env()
    if client is None:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set; nothing to check.")
        return 0
    text, over = report(client, args.warn_share)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
