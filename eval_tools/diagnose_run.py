"""Diagnose one already-forecast question: an objective checklist over its saved run.

    # a run folder on disk (docs/runs/<ts>/<qid>_<title>/)
    poetry run python eval_tools/diagnose_run.py local <question_dir> [--no-qwen] [--publish]

    # a run stored in the forecast library (latest run, or a given run id)
    poetry run python eval_tools/diagnose_run.py fetch --question-id 46133 [--run-id gh-...] [--publish]

Writes diagnostics.json, diagnostics.md and lineage.json (the run's flow map,
API calls and evidence lifecycle; spec in diagnostics/LINEAGE.md) into the
question folder. With --publish, upserts one row per check into Supabase
`run_diagnostics`, the lineage into `run_lineage`, and uploads diagnostics.md
plus every step's input/output file (<blob_prefix>/steps/<file>.gz) next to
the run's files.

Never forecasts and never calls a paid model: the paid keys are blanked before
anything is imported, and the Qwen tasks assert a SoCLaaS route per call.
"""
from __future__ import annotations

import os

# Before ANY bot import: dotenv never overrides an existing variable, so blank
# values here survive config loading and make a stray paid call fail, not spend.
for _key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_key] = ""
for _key in ("TIER1_MODEL", "TIER2_MODEL", "QWEN_RESEARCH_LABELS"):
    os.environ.pop(_key, None)
os.environ.setdefault("QWEN_LADDER", "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import tarfile  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from diagnostics import checks_lineage, lineage  # noqa: E402
from diagnostics.loader import load, long_path  # noqa: E402
from diagnostics.result import sort_key  # noqa: E402
from diagnostics.runner import render_markdown, run_all  # noqa: E402
from eval_tools.publish_runs import BUCKET, _gzip, _sanitize  # noqa: E402
from eval_tools.supabase_rest import SupabaseError, SupabaseREST  # noqa: E402

TABLE = "run_diagnostics"
LINEAGE_TABLE = "run_lineage"
_FILES = ("forecast.json", "research.md.gz", "runs.md.gz", "audit.md.gz", "evolution.md.gz")


def _write(path: str, data: bytes) -> None:
    os.makedirs(long_path(os.path.dirname(path)), exist_ok=True)
    with open(long_path(path), "wb") as handle:
        handle.write(data)


def fetch(client: SupabaseREST, question_id: int, run_id: str | None, out_root: str) -> tuple[str, str, str]:
    """Download a stored run into out_root. Returns (question_dir, run_id, blob_prefix)."""
    params = {"select": "run_id,question_id,blob_prefix,run_at", "question_id": f"eq.{question_id}",
              "order": "run_at.desc", "limit": "1"}
    if run_id:
        params["run_id"] = f"eq.{run_id}"
    rows = client.select("forecasts", params)
    if not rows:
        raise SystemExit(f"No forecast for question {question_id}"
                         + (f" in run {run_id}" if run_id else "") + " in the library.")
    run_id, prefix = rows[0]["run_id"], rows[0]["blob_prefix"]
    run_dir = os.path.join(out_root, run_id)
    qdir = os.path.join(run_dir, f"q{question_id}")
    for name in _FILES:
        try:
            data = client.download(BUCKET, f"{prefix}/{name}")
        except SupabaseError as exc:
            print(f"  (missing {name}: {str(exc)[:120]})")
            continue
        if name.endswith(".gz"):
            data, name = gzip.decompress(data), name[:-3]
        _write(os.path.join(qdir, name), data)
    try:
        trace = client.download(BUCKET, f"{prefix}/trace.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(trace), mode="r:gz") as tar:
            tar.extractall(long_path(os.path.join(qdir, "trace")), filter="data")
    except SupabaseError as exc:
        print(f"  (missing trace: {str(exc)[:120]})")
    try:
        _write(os.path.join(run_dir, "run.log"),
               gzip.decompress(client.download(BUCKET, f"runs/{run_id}/run.log.gz")))
    except SupabaseError:
        print("  (no run.log stored for this run)")
    return qdir, run_id, prefix


def publish(client: SupabaseREST, art, results, markdown: str, lin: dict, run_id: str, prefix: str | None) -> None:
    question_id = int(art.forecast.get("question_id"))
    rows = [_sanitize({"run_id": run_id, "question_id": question_id, **r.as_row()}) for r in results]
    client.upsert(TABLE, rows, on_conflict="run_id,question_id,check_id")
    prefix = prefix or f"runs/{run_id}/{question_id}"
    client.upload(BUCKET, f"{prefix}/diagnostics.md.gz", _gzip(markdown.encode("utf-8")), "application/gzip")
    # Step files one by one: the step page opens a single input/output, and the
    # run's trace.tar.gz cannot be read file by file from a browser.
    files = lineage.payload_files(lin)
    for name in files:
        path = long_path(os.path.join(art.question_dir, "trace", name))
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                text = lineage.redact(handle.read().decode("utf-8", errors="replace"))
            client.upload(BUCKET, f"{prefix}/steps/{name}.gz", _gzip(text.encode("utf-8")), "application/gzip")
    try:
        client.upsert(LINEAGE_TABLE, [_sanitize({
            "run_id": run_id, "question_id": question_id, "schema_version": lin["schema_version"],
            "lineage_source": lin["lineage_source"], "lineage": json.loads(lineage.dumps(lin)),
        })], on_conflict="run_id,question_id")
    except SupabaseError as exc:  # the checks above are already stored
        print(f"Lineage NOT stored ({str(exc)[:200]}). If the table is missing, apply "
              "db/migrations/004_run_lineage.sql in the Supabase SQL editor and re-run.")
    print(f"Published {len(rows)} checks to {TABLE}, the lineage to {LINEAGE_TABLE}, "
          f"{prefix}/diagnostics.md.gz and {len(files)} step files under {prefix}/steps/")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    local = sub.add_parser("local")
    local.add_argument("question_dir")
    local.add_argument("--run-log")
    remote = sub.add_parser("fetch")
    remote.add_argument("--question-id", type=int, required=True)
    remote.add_argument("--run-id")
    remote.add_argument("--out", default="diagnostics_runs")
    for p in (local, remote):
        p.add_argument("--precompression", help="data/qwen-precompression folder, if available")
        p.add_argument("--no-qwen", action="store_true")
        p.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)

    client = SupabaseREST.from_env()
    prefix = None
    if args.mode == "fetch":
        if client is None:
            raise SystemExit("fetch needs SUPABASE_URL and SUPABASE_SERVICE_KEY")
        qdir, run_id, prefix = fetch(client, args.question_id, args.run_id, args.out)
        art = load(qdir, precompression_dir=args.precompression)
    else:
        qdir = args.question_dir
        art = load(qdir, run_log=args.run_log, precompression_dir=args.precompression)
        run_id = art.run_id
    if not art.forecast:
        raise SystemExit(f"No forecast.json in {qdir}")

    results = await run_all(art, use_qwen=not args.no_qwen)
    lin = lineage.build(art, results, files_prefix=prefix or (f"runs/{run_id}/{art.forecast.get('question_id')}"
                                                            if run_id else None))
    results = sorted(results + checks_lineage.run(lin), key=sort_key)
    markdown = render_markdown(art, results)
    _write(os.path.join(qdir, "lineage.json"), lineage.dumps(lin).encode("utf-8"))
    _write(os.path.join(qdir, "diagnostics.json"),
           json.dumps(_sanitize([r.as_row() for r in results]), indent=1, ensure_ascii=False).encode("utf-8"))
    _write(os.path.join(qdir, "diagnostics.md"), markdown.encode("utf-8"))
    print(markdown)

    if args.publish:
        if client is None:
            print("--publish skipped: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
        elif not run_id:
            print("--publish skipped: no run id in forecast.json provenance")
        else:
            publish(client, art, results, markdown, lin, run_id, prefix)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
