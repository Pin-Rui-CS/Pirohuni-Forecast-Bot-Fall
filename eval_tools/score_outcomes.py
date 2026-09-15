"""Fill in outcomes and scores for the forecast library. Run daily.

Two passes:

  1. Check questions whose outcome isn't known yet (`questions_to_check`,
     stalest first) against Metaculus and upsert `outcomes`. Capped per run,
     because Metaculus calls are deliberately spaced several seconds apart.
  2. Score every forecast on a resolved question that has no score yet
     (`forecasts_to_score`) and upsert `scores`. No network beyond Supabase;
     this also catches forecasts published after their question resolved.

Scoring reuses eval_tools/score_forecasts.score_record, so the library and the
offline report cannot disagree about a score.

Usage:
    poetry run python eval_tools/score_outcomes.py [--max-checks N]

Needs METACULUS_TOKEN, SUPABASE_URL and SUPABASE_SERVICE_KEY. Without the
Supabase pair it prints a notice and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv.load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from eval_tools.score_forecasts import score_record  # noqa: E402
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402
from metaculus_client import get_post_details  # noqa: E402

DEFAULT_MAX_CHECKS = 150
_PAGE = 200


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def refresh_outcomes(client: SupabaseREST, max_checks: int) -> tuple[int, int]:
    """Returns (checked, newly_resolved)."""
    pending = client.select(
        "questions_to_check",
        {"select": "question_id,post_id", "limit": max_checks},
    )
    checked = resolved = 0
    for row in pending:
        post_id = row.get("post_id")
        if post_id is None:
            continue
        try:
            post = await get_post_details(int(post_id))
        except Exception as exc:  # noqa: BLE001 - one question must not stop the pass
            print(f"[outcomes] q{row['question_id']}: could not fetch ({exc})")
            continue
        question = post.get("question") or {}
        status = question.get("status")
        resolution = question.get("resolution")
        client.upsert(
            "outcomes",
            [{
                "question_id": row["question_id"],
                "post_id": post_id,
                "status": status,
                "resolution": None if resolution is None else str(resolution),
                "resolved_at": question.get("actual_resolve_time"),
                "checked_at": _now(),
            }],
            on_conflict="question_id",
        )
        checked += 1
        if status == "resolved":
            resolved += 1
            print(f"[outcomes] q{row['question_id']} resolved: {resolution!r}")
    return checked, resolved


def score_pending(client: SupabaseREST) -> tuple[int, int]:
    """Returns (scored, unscorable)."""
    scored = unscorable = 0
    offset = 0
    # A safety stop: if an upsert ever "succeeded" without the row leaving the
    # view (a permissions misconfiguration, say), the window would never move.
    for _ in range(10_000):
        page = client.select(
            "forecasts_to_score",
            {"select": "run_id,question_id,resolution,raw",
             "order": "run_id,question_id", "limit": _PAGE, "offset": offset},
        )
        rows = []
        for row in page:
            result = score_record(row["raw"], str(row["resolution"]))
            if result is None:
                # Annulled, ambiguous or abstained. It stays in the view, which
                # is cheap: no network is spent re-checking it.
                unscorable += 1
                continue
            metric, score = result
            rows.append({
                "run_id": row["run_id"],
                "question_id": row["question_id"],
                "metric": metric,
                "score": score,
                "resolution": str(row["resolution"]),
                "scored_at": _now(),
            })
        if rows:
            client.upsert("scores", rows, on_conflict="run_id,question_id")
            scored += len(rows)
        if len(page) < _PAGE:
            break
        # Scored rows leave the view, so only unscorable ones shift the window.
        offset += len(page) - len(rows)
    return scored, unscorable


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-checks", type=int, default=DEFAULT_MAX_CHECKS)
    args = parser.parse_args(argv)

    client = SupabaseREST.from_env()
    if client is None:
        print("[outcomes] SUPABASE_URL / SUPABASE_SERVICE_KEY not set; skipping.")
        return 0
    with client:
        checked, newly_resolved = await refresh_outcomes(client, args.max_checks)
        scored, unscorable = score_pending(client)
    print(
        f"[outcomes] checked {checked} question(s), {newly_resolved} resolved; "
        f"scored {scored} forecast(s), {unscorable} unscorable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
