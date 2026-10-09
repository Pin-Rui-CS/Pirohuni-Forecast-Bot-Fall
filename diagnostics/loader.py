"""Load one question's saved run artifacts into a single object.

Every field is optional in practice: older runs lack newer files, and a run
fetched from the forecast library has no precompression folder. A missing piece
is an empty value, never an exception -- the check that needs it reports
"skipped" instead.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_PROVIDER_RE = re.compile(r"^## Provider:\s*(?P<name>.+?)\s*$", re.M)
_RUN_RE = re.compile(r"^## Run (\d+)\s*$", re.M)
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ")


def long_path(path: str) -> str:
    """Windows refuses paths over 260 chars unless they carry the \\\\?\\ prefix.

    Run folders are named after the question title, so trace files routinely
    exceed it (46133's do).
    """
    path = os.path.abspath(path)
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        return "\\\\?\\" + path
    return path


def _read(path: str) -> str:
    try:
        with open(long_path(path), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _section(text: str, heading: str, *, level: int = 2) -> str:
    """The body of '## heading' up to the next heading of the same level."""
    marks = "#" * level
    pattern = re.compile(rf"^{marks} {re.escape(heading)}[^\n]*\n(.*?)(?=^{marks} |\Z)", re.M | re.S)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def bullets(text: str) -> list[str]:
    return [line.strip()[2:].strip() for line in text.splitlines()
            if line.strip().startswith(("- ", "* "))]


@dataclass
class RunArtifacts:
    question_dir: str
    forecast: dict = field(default_factory=dict)
    research_md: str = ""
    runs_md: str = ""
    audit_md: str = ""
    trace_events: list[dict] = field(default_factory=list)
    run_log_lines: list[str] = field(default_factory=list)   # this question's time window
    run_log_all: list[str] = field(default_factory=list)     # whole run, for run-wide causes
    qwen_attempts: list[dict] = field(default_factory=list)
    precompression: dict[str, dict] = field(default_factory=dict)

    # ---- research.md ---------------------------------------------------------
    @property
    def evidence_plan(self) -> str:
        # The plan uses '##' headings itself, so take the whole provider block
        # (or, failing that, everything up to the artifact check).
        for name, content in self.providers:
            if name == "Evidence Plan":
                return content
        start = self.research_md.find("## Evidence Plan")
        end = self.research_md.find("## Required Artifact Check")
        return self.research_md[start:end] if start >= 0 and end > start else ""

    @property
    def brief(self) -> str:
        start = self.research_md.find("## Compiled Brief (sent to forecaster)")
        if start < 0:
            return ""
        body = self.research_md[start:]
        nxt = _PROVIDER_RE.search(body)
        return body[: nxt.start()] if nxt else body

    @property
    def providers(self) -> list[tuple[str, str]]:
        matches = list(_PROVIDER_RE.finditer(self.research_md))
        out = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(self.research_md)
            out.append((match.group("name"), self.research_md[match.end():end].strip()))
        return out

    def plan_list(self, heading: str) -> list[str]:
        return bullets(_section(self.evidence_plan, heading))

    # ---- runs.md ---------------------------------------------------------------
    @property
    def forecast_runs(self) -> list[tuple[int, str]]:
        matches = list(_RUN_RE.finditer(self.runs_md))
        final = re.search(r"^## Final\s*$", self.runs_md, re.M)
        out = []
        for i, match in enumerate(matches):
            end = (matches[i + 1].start() if i + 1 < len(matches)
                   else (final.start() if final else len(self.runs_md)))
            out.append((int(match.group(1)), self.runs_md[match.end():end]))
        return out

    # ---- audit.md ---------------------------------------------------------------
    @property
    def ledger(self) -> list[dict]:
        """Rows of the audit 'All URL events' table."""
        body = _section(self.audit_md, "All URL events", level=3)
        rows = []
        for line in body.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 9 or not cells[0].isdigit():
                continue
            rows.append({"tool": cells[1], "phase": cells[2], "round": cells[3],
                         "role": cells[4], "engine": cells[5], "status": cells[6],
                         "chars": int(cells[7]) if cells[7].isdigit() else None,
                         "url": "|".join(cells[8:])})
        return rows

    @property
    def fetch_budget(self) -> str:
        return _section(self.audit_md, "Fetch Budget (this question)")

    # ---- trace -------------------------------------------------------------------
    def events(self, stage: str | None = None) -> list[dict]:
        return [e for e in self.trace_events if stage is None or e.get("stage") == stage]

    def payload(self, event: dict) -> str:
        name = event.get("payload_file") or ""
        return _read(os.path.join(self.question_dir, "trace", name)) if name else ""

    # ---- forecast.json shortcuts ------------------------------------------------
    @property
    def question(self) -> dict:
        return self.forecast.get("question_details") or {}

    @property
    def llm_calls(self) -> list[dict]:
        return self.forecast.get("llm_calls") or []

    @property
    def run_id(self) -> str:
        return ((self.forecast.get("provenance") or {}).get("run_id")) or ""


def _question_window(events: list[dict], forecast: dict) -> tuple[dt.datetime, dt.datetime] | None:
    stamps = []
    for event in events:
        try:
            stamps.append(dt.datetime.fromisoformat(event["ts"]))
        except (KeyError, ValueError, TypeError):
            continue
    if not stamps:
        return None
    total = float((forecast.get("timings") or {}).get("total_seconds") or 0)
    start = min(stamps) - dt.timedelta(seconds=120)
    end = max(max(stamps), min(stamps) + dt.timedelta(seconds=total)) + dt.timedelta(seconds=600)
    return start, end


def _slice_run_log(text: str, window) -> list[str]:
    """run.log lines inside this question's time window.

    run.log interleaves concurrent questions, so this slice can contain another
    question's lines too; checks that use it say so and match on this
    question's URLs or labels where they can.
    """
    if not text or not window:
        return []
    start, end = window
    out, keep = [], False
    for line in text.splitlines():
        match = _LOG_TS.match(line)
        if match:
            try:
                keep = start <= dt.datetime.fromisoformat(match.group(1)) <= end
            except ValueError:
                keep = False
        if keep:
            out.append(line)
    return out


def load(question_dir: str, *, run_log: str | None = None,
         precompression_dir: str | None = None) -> RunArtifacts:
    art = RunArtifacts(question_dir=question_dir)
    raw = _read(os.path.join(question_dir, "forecast.json"))
    art.forecast = json.loads(raw) if raw.strip() else {}
    art.research_md = _read(os.path.join(question_dir, "research.md"))
    art.runs_md = _read(os.path.join(question_dir, "runs.md"))
    art.audit_md = _read(os.path.join(question_dir, "audit.md"))
    for line in _read(os.path.join(question_dir, "trace", "trace.jsonl")).splitlines():
        try:
            art.trace_events.append(json.loads(line))
        except ValueError:
            continue
    for line in _read(os.path.join(question_dir, "trace", "qwen_attempts.jsonl")).splitlines():
        try:
            art.qwen_attempts.append(json.loads(line))
        except ValueError:
            continue

    log_path = run_log or os.path.join(os.path.dirname(os.path.abspath(question_dir)), "run.log")
    log_text = _read(log_path)
    art.run_log_all = log_text.splitlines()
    art.run_log_lines = _slice_run_log(log_text, _question_window(art.trace_events, art.forecast))

    if precompression_dir and os.path.isdir(long_path(precompression_dir)):
        wanted = {os.path.basename(str((e.get("meta") or {}).get("artifact_dir") or "").rstrip("/\\"))
                  for e in art.events("precompress")} - {""}
        for name in wanted:
            folder = os.path.join(precompression_dir, name)
            manifest = _read(os.path.join(folder, "manifest.json"))
            if not manifest:
                continue
            entry: dict[str, Any] = {"manifest": json.loads(manifest), "results": {}}
            for effort in ("xhigh", "medium"):
                result = _read(os.path.join(folder, effort, "result.json"))
                if result:
                    entry["results"][effort] = json.loads(result)
            art.precompression[name] = entry
    return art
