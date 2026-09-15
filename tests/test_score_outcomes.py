"""Offline checks for the daily outcome-scoring job. No network, no database.

    python tests/test_score_outcomes.py

Simulates Supabase's `forecasts_to_score` view -- a relation that rows LEAVE
as soon as they are scored -- to check that pagination neither skips nor
repeats forecasts, including when unscorable (annulled) ones are mixed in.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from eval_tools import score_outcomes  # noqa: E402
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        FAILURES.append(message)


class FakeSupabase:
    """Just enough PostgREST: two views computed live from in-memory tables."""

    def __init__(self, forecasts: list[dict], outcomes: dict[int, dict]) -> None:
        self.forecasts = forecasts
        self.outcomes = outcomes
        self.scores: dict[tuple, dict] = {}
        self.score_upserts = 0

    def _forecasts_to_score(self) -> list[dict]:
        rows = []
        for f in sorted(self.forecasts, key=lambda r: (r["run_id"], r["question_id"])):
            outcome = self.outcomes.get(f["question_id"])
            if outcome and outcome["status"] == "resolved" and (f["run_id"], f["question_id"]) not in self.scores:
                rows.append({**f, "resolution": outcome["resolution"]})
        return rows

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if request.method == "GET" and path.endswith("/forecasts_to_score"):
            offset, limit = int(params.get("offset", 0)), int(params["limit"])
            return httpx.Response(200, json=self._forecasts_to_score()[offset:offset + limit])
        if request.method == "GET" and path.endswith("/questions_to_check"):
            pending = [{"question_id": q, "post_id": q + 1000} for q in sorted({f["question_id"] for f in self.forecasts})
                       if self.outcomes.get(q, {}).get("status") != "resolved"]
            return httpx.Response(200, json=pending[: int(params["limit"])])
        if request.method == "POST" and path.endswith("/scores"):
            self.score_upserts += 1
            for row in json.loads(request.content):
                self.scores[(row["run_id"], row["question_id"])] = row
            return httpx.Response(201)
        if request.method == "POST" and path.endswith("/outcomes"):
            for row in json.loads(request.content):
                self.outcomes[row["question_id"]] = row
            return httpx.Response(201)
        return httpx.Response(404, text=f"unexpected {request.method} {path}")


def binary(run: int, question: int, p: float) -> dict:
    return {"run_id": f"r{run:04d}", "question_id": question,
            "raw": {"question_type": "binary", "final_forecast": p}}


def main() -> int:
    print("\npagination over a view that shrinks as rows are scored")
    # 450 scorable forecasts across three pages, with annulled ones interleaved
    # so the offset has to account for rows that stay behind.
    forecasts = [binary(i, 1 + i % 3, 0.25) for i in range(450)]
    # Question 99 is annulled. Same run ids as scorable rows, so in (run_id,
    # question_id) order they sit BETWEEN scorable rows on every page.
    forecasts += [binary(i, 99, 0.5) for i in range(0, 450, 3)]
    outcomes = {q: {"status": "resolved", "resolution": "yes"} for q in (1, 2, 3)}
    outcomes[99] = {"status": "resolved", "resolution": "annulled"}
    fake = FakeSupabase(forecasts, outcomes)
    client = SupabaseREST("https://proj.supabase.co", "sb_secret_x", transport=httpx.MockTransport(fake))
    with client:
        scored, unscorable = score_outcomes.score_pending(client)
    check(scored == 450, f"every scorable forecast scored exactly once ({scored})")
    check(len(fake.scores) == 450, "no duplicates, none skipped")
    ordered = [(f["run_id"], f["question_id"]) for f in fake._forecasts_to_score()]
    check(all(key[1] == 99 for key in ordered) and len(ordered) == 150,
          "only the 150 annulled rows remain in the view afterwards")
    check(unscorable == 150, f"annulled forecasts counted once each ({unscorable})")
    check(all(abs(row["score"] - 0.5625) < 1e-12 and row["metric"] == "brier" for row in fake.scores.values()),
          "scores computed with the shared scorer")

    print("\nre-running finds nothing new")
    with SupabaseREST("https://proj.supabase.co", "sb_secret_x", transport=httpx.MockTransport(fake)) as client:
        before = fake.score_upserts
        scored_again, _ = score_outcomes.score_pending(client)
    check(scored_again == 0 and fake.score_upserts == before, "second run upserts nothing")

    print("\noutcome refresh")
    fake2 = FakeSupabase([binary(1, 7, 0.9), binary(2, 8, 0.1)], {})

    async def fake_post_details(post_id: int) -> dict:
        question_id = post_id - 1000
        if question_id == 7:
            return {"question": {"status": "resolved", "resolution": "no", "actual_resolve_time": "2026-10-01T00:00:00Z"}}
        return {"question": {"status": "open", "resolution": None}}

    score_outcomes.get_post_details = fake_post_details
    with SupabaseREST("https://proj.supabase.co", "sb_secret_x", transport=httpx.MockTransport(fake2)) as client:
        checked, resolved = asyncio.run(score_outcomes.refresh_outcomes(client, max_checks=10))
        scored, _ = score_outcomes.score_pending(client)
    check(checked == 2 and resolved == 1, f"both checked, one resolved ({checked}, {resolved})")
    check(fake2.outcomes[8]["resolution"] is None, "an open question stores no resolution")
    check(scored == 1 and abs(fake2.scores[("r0001", 7)]["score"] - 0.81) < 1e-12,
          "the newly resolved forecast is scored in the same run")

    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
