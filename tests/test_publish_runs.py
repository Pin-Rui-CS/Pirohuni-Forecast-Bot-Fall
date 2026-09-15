"""Offline checks for the forecast-library publisher. No network, no database.

    python tests/test_publish_runs.py

Builds a synthetic docs/runs tree -- one current (schema v2) record and one
legacy (v1) record with each historical usage-table layout -- publishes it
through an in-memory HTTP transport, and asserts on the requests Supabase
would receive.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from eval_tools import publish_runs  # noqa: E402
from eval_tools.score_forecasts import score_record  # noqa: E402
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        FAILURES.append(message)


# Header layouts the rendered usage table has had, oldest first.
_OLD_HEADER = ("| no. | name of task | input characters | input tokens | output characters | output tokens "
               "| reasoning tokens | native in | native out | cached in | cost usd | seconds | model used |")
_NEW_HEADER = ("| no. | name of task | input characters | input tokens | output characters | output tokens "
               "| reasoning tokens | native in | native out | cached in | cache write | cost usd | quota ud "
               "| seconds | endpoint | model used |")


def _write(path: str, content: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as handle:
        handle.write(content)


def build_tree(root: str) -> None:
    run = os.path.join(root, "2026-09-15_10-00")
    _write(os.path.join(run, "run.log"), "run log\n")

    # Current record: provenance + structured llm_calls, and a NaN that Python's
    # json writes but Postgres jsonb would reject.
    q2 = os.path.join(run, "45001_Numeric_question")
    v2 = {
        "schema_version": 2, "question_id": 45001, "post_id": 46001, "title": "Numeric question",
        "question_type": "numeric", "run_timestamp": "2026-09-15_10-00",
        "provenance": {
            "run_id": "gh-999-1", "code_sha": "abc123", "workflow": "main", "event": "schedule",
            "run_url": "https://github.com/x/y/actions/runs/999",
            "routing": {"tier1_model": "openai/gpt-6-astra", "tier2_model": "qwen3.8:27b"},
            "cli": {"mode": "tournament", "num_runs": 3},
        },
        "final_forecast": [0.0, 0.5, float("nan"), 1.0], "total_cost_usd": 1.25,
        "abstained": False, "submitted": True, "estimated_tokens": 1000,
        "llm_calls": [
            {"no": 1, "name_of_task": "compiler/research-brief", "model_used": "openai/gpt-6-astra",
             "endpoint": "OpenRouter", "cost_source": "native", "input_tokens": 40, "output_tokens": 5,
             "native_input_tokens": 120000, "native_output_tokens": 15000, "reasoning_tokens": 0,
             "cached_input_tokens": 0, "cache_write_tokens": 0, "cost_usd": 1.25,
             "quota_microdollars": 0.0, "duration_seconds": 30.0, "input_characters": 999},
            {"no": 2, "name_of_task": "serp/ranking", "model_used": "qwen3.8:27b",
             "endpoint": "SoCLaaS", "cost_source": "quota", "input_tokens": 40, "output_tokens": 5,
             "native_input_tokens": 40000, "native_output_tokens": 2000, "reasoning_tokens": 0,
             "cached_input_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
             "quota_microdollars": 9600.0, "duration_seconds": 3.9, "input_characters": 999},
        ],
    }
    os.makedirs(q2, exist_ok=True)
    with open(os.path.join(q2, "forecast.json"), "w", encoding="utf-8") as f:
        json.dump(v2, f)  # allow_nan=True, exactly as artifacts.py writes it
    _write(os.path.join(q2, "research.md"), "# research\n" * 50)
    _write(os.path.join(q2, "runs.md"), "# runs\n")
    _write(os.path.join(q2, "trace", "001_scrape.md"), "payload")

    # Legacy record: no provenance, costs only in the rendered table (old layout).
    q1 = os.path.join(run, "44879_CISA_KEV")
    v1 = {
        "question_id": 44879, "post_id": 45879, "title": "CISA KEV", "question_type": "binary",
        "run_timestamp": "2026-09-15_10-00", "final_forecast": 0.42, "submitted": True,
        "usage_yaml_table": "\n".join([
            "openrouter_llm_usage:",
            "  total_cost_usd: 2.183767  # OpenRouter-reported",
            "  table: |",
            f"    {_OLD_HEADER}",
            "    | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            "    | 1 | compiler/research-brief | 126 | 40 | 2 | 1 | 0 | 134756 | 15437 | 0 | 0.513000 | 30.1 | anthropic/claude-opus-5 |",
            "    | 2 | serp/ranking | 126 | 40 | 4 | 2 | 0 | 57 | 28 | 0 | 0.090000 | 3.9 | anthropic/claude-sonnet-5 |",
        ]),
    }
    _write(os.path.join(q1, "forecast.json"), json.dumps(v1))
    _write(os.path.join(q1, "research.md"), "# legacy research\n")


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(201 if request.method == "POST" else 200, json=[])


def run_publish(root: str, key: str) -> Recorder:
    recorder = Recorder()
    client = SupabaseREST("https://proj.supabase.co", key, transport=httpx.MockTransport(recorder))
    with client:
        published, failed = publish_runs.publish([root], client, out=io.StringIO())
    check(published == 2 and failed == 0, f"published 2, failed 0 (got {published}, {failed})")
    return recorder


def main() -> int:
    with tempfile.TemporaryDirectory() as root:
        build_tree(root)

        print("\nrequests and ordering")
        recorder = run_publish(root, "sb_secret_abc")
        paths = [r.url.path for r in recorder.requests]
        uploads = [p for p in paths if p.startswith("/storage/")]
        upserts = [p for p in paths if p.startswith("/rest/")]
        check(len(uploads) >= 5 and len(upserts) >= 4, f"{len(uploads)} uploads, {len(upserts)} upserts")
        first_forecast_upsert = paths.index("/rest/v1/forecasts")
        blob_paths_before = [p for p in paths[:first_forecast_upsert] if "45001" in p or "44879" in p]
        check(len(blob_paths_before) >= 1, "files are uploaded before the first forecasts row")

        print("\nauthentication headers")
        first = recorder.requests[0]
        check(first.headers.get("apikey") == "sb_secret_abc", "secret key sent on apikey header")
        check("authorization" not in first.headers, "no Bearer header for a non-JWT secret key")
        legacy = run_publish(root, "eyJhbGciOi.legacy.jwt")
        check(legacy.requests[0].headers.get("authorization") == "Bearer eyJhbGciOi.legacy.jwt",
              "legacy JWT key also sent as Bearer")

        print("\nupserts are keyed for idempotency")
        conflicts = {r.url.path: r.url.params.get("on_conflict") for r in recorder.requests
                     if r.url.path.startswith("/rest/")}
        check(conflicts.get("/rest/v1/forecasts") == "run_id,question_id", "forecasts on (run_id, question_id)")
        check(conflicts.get("/rest/v1/llm_calls") == "run_id,question_id,call_no", "llm_calls on call_no")
        check(all("merge-duplicates" in r.headers.get("prefer", "")
                  for r in recorder.requests if r.url.path.startswith("/rest/")), "merge-duplicates on every upsert")
        again = run_publish(root, "sb_secret_abc")
        body = lambda rec: [(r.url.path, r.content) for r in rec.requests if r.url.path.startswith("/rest/")]  # noqa: E731
        check(body(recorder) == body(again), "publishing twice sends identical rows")

        print("\nv2 record")
        item = publish_runs.prepare(os.path.join(root, "2026-09-15_10-00", "45001_Numeric_question", "forecast.json"),
                                    publish_runs.Overrides())
        row = item.forecast_row
        check(row["run_id"] == "gh-999-1" and row["schema_version"] == 2, "run_id and schema version from provenance")
        check(row["tier1_model"] == "openai/gpt-6-astra" and row["tier2_model"] == "qwen3.8:27b", "tier models promoted")
        check(row["raw"]["final_forecast"][2] is None, "NaN in raw replaced with null")
        json.dumps(row["raw"], allow_nan=False)  # raises if any non-finite value survived
        check(len(item.llm_call_rows) == 2 and item.llm_call_rows[1]["quota_microdollars"] == 9600.0,
              "structured llm_calls mapped, including quota")
        check("input_characters" not in item.llm_call_rows[0], "only llm_calls columns sent")
        check(len({tuple(sorted(r)) for r in item.llm_call_rows}) == 1, "llm_calls rows are uniform")
        names = {p.rsplit("/", 1)[1] for p, _, _ in item.blobs}
        check(names == {"forecast.json", "research.md.gz", "runs.md.gz", "trace.tar.gz"},
              f"expected files present, missing ones skipped: {sorted(names)}")
        research = next(d for p, d, _ in item.blobs if p.endswith("research.md.gz"))
        check(gzip.decompress(research).startswith(b"# research"), "markdown round-trips through gzip")
        trace = next(d for p, d, _ in item.blobs if p.endswith("trace.tar.gz"))
        with tarfile.open(fileobj=io.BytesIO(trace)) as tar:
            check(tar.getnames() == ["001_scrape.md"], "trace tarball holds the payload files")

        print("\nv1 legacy record")
        path = os.path.join(root, "2026-09-15_10-00", "44879_CISA_KEV", "forecast.json")
        item = publish_runs.prepare(path, publish_runs.Overrides(run_id="gh-555-1", code_sha="old", workflow="main"))
        row = item.forecast_row
        check(row["schema_version"] == 1 and row["run_id"] == "gh-555-1", "legacy record takes the override run_id")
        check(row["code_sha"] == "old" and row["tier1_model"] is None, "overrides fill gaps; unknown models stay null")
        check(row["total_cost_usd"] == 2.183767, "total cost recovered from the rendered summary")
        check(len(item.llm_call_rows) == 2, "calls recovered from the old-layout table")
        check(item.llm_call_rows[0]["native_input_tokens"] == 134756 and item.llm_call_rows[0]["cost_usd"] == 0.513,
              "columns mapped by header name")
        check(item.llm_call_rows[0]["cache_write_tokens"] is None, "a column the old layout lacks is null")
        check(row["abstained"] is False, "abstained inferred from final_forecast")

        new_layout = "\n".join([
            f"    {_NEW_HEADER}",
            "    | --- |",
            "    | 1 | serp/ranking | 126 | 40 | 4 | 2 | 0 | 57 | 28 | 0 | 0 | 0.000000 | 36.3 | 3.9 | SoCLaaS | qwen3.8:27b |",
        ])
        calls = publish_runs.calls_from_rendered_table(new_layout)
        check(calls and calls[0]["quota_microdollars"] == 36.3 and calls[0]["endpoint"] == "SoCLaaS",
              "current table layout parsed too")

        print("\nnot configured")
        saved = {k: os.environ.pop(k, None) for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")}
        try:
            check(SupabaseREST.from_env() is None, "no client without credentials")
        finally:
            os.environ.update({k: v for k, v in saved.items() if v is not None})

        print("\nscoring dispatch")
        check(score_record({"question_type": "binary", "final_forecast": 0.25}, "yes") == ("brier", 0.5625),
              "binary Brier")
        check(score_record({"question_type": "binary", "final_forecast": None}, "yes") is None, "abstained unscored")
        check(score_record({"question_type": "binary", "final_forecast": 0.25}, "annulled") is None, "annulled unscored")

    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
