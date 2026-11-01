"""Score each ensemble model's own forecast on resolved questions.

Every forecast.json keeps the individual runs: ``extra.ensemble`` (model,
backup_for, dropped, and a binary run's ``probability``) and ``run_values``
(a multiple-choice run's option probabilities or a numeric run's parsed
distribution, one per non-dropped ensemble record, in order). The forecast
library holds each forecast.json verbatim and score_outcomes.py keeps the
outcomes current, so this report needs no LLM and no forecast files.

For the last N resolved questions (the final run on each: the latest submitted
run, else the latest run) it scores every model's run and the bot's submitted
forecast with the same functions the library uses (score_forecasts.py), then
compares every pair of models on the questions both forecast.

The Metaculus community prediction is not included: the API returns empty
aggregations to the bot's account, even for public questions.

Usage:
    poetry run python eval_tools/model_scores.py [--last-n 50]
        [--question-type all|binary|multiple_choice|numeric]
        [--tournament TEXT] [--since YYYY-MM-DD] [--out model-scores]

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY. Writes <out>/summary.md and
<out>/per_forecast.csv, and appends the summary to $GITHUB_STEP_SUMMARY when set.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import datetime
import math
import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv.load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from eval_tools.score_forecasts import (  # noqa: E402
    UNSCORABLE_RESOLUTIONS,
    crps_from_cdf,
    score_binary,
    score_multiple_choice,
)
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402
from bot.forecasters.numeric import nominal_grid, spec_to_cdf  # noqa: E402

BOT = "bot (submitted)"
DEFAULT_LAST_N = 50
BOOTSTRAP_RESAMPLES = 2000
# Below this many shared questions a head-to-head is flagged as a small sample.
SMALL_N = 10
LOG_CLIP = 0.001
_PAGE = 200
_ID_CHUNK = 100
TYPE_GROUPS = {
    "binary": ("binary",),
    "multiple_choice": ("multiple_choice",),
    "numeric": ("numeric", "discrete"),
}
GROUP_TITLES = {"binary": "Binary", "multiple_choice": "Multiple choice", "numeric": "Numeric and discrete"}
# metric -> (label, lower is better)
METRICS = {
    "brier": ("Brier", True),
    "log": ("Log score", False),
    "crps": ("CRPS", True),
}
# Only the parts of forecast.json this report reads.
_FORECAST_SELECT = (
    "run_id,question_id,post_id,question_type,title,run_at,submitted,"
    "extra:raw->extra,run_values:raw->run_values,final_forecast:raw->final_forecast,"
    "scaling:raw->question_details->scaling,tournaments:raw->tournaments"
)


def _group_of(question_type: str | None) -> str | None:
    return next((g for g, types in TYPE_GROUPS.items() if question_type in types), None)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _paged(client: SupabaseREST, relation: str, params: dict) -> list[dict]:
    rows, offset = [], 0
    while True:
        page = client.select(relation, {**params, "limit": _PAGE, "offset": offset})
        rows.extend(page)
        if len(page) < _PAGE:
            return rows
        offset += _PAGE


def resolved_outcomes(client: SupabaseREST, since: str | None) -> list[dict]:
    """Scorable resolved questions, most recently resolved first."""
    params = {"select": "question_id,post_id,resolution,resolved_at",
              "status": "eq.resolved", "order": "resolved_at.desc.nullslast,question_id.desc"}
    if since:
        params["resolved_at"] = f"gte.{since}"
    return [r for r in _paged(client, "outcomes", params)
            if r.get("resolution") is not None and str(r["resolution"]) not in UNSCORABLE_RESOLUTIONS]


def final_run(rows: list[dict]) -> dict:
    """The run whose forecast stands: the latest submitted one, else the latest."""
    pool = [r for r in rows if r.get("submitted")] or rows
    return max(pool, key=lambda r: r.get("run_at") or "")


def matches_tournament(row: dict, text: str | None) -> bool:
    if not text:
        return True
    needle = text.strip().lower()
    return any(needle in str(t.get(k) or "").lower()
               for t in row.get("tournaments") or [] if isinstance(t, dict) for k in ("name", "slug"))


def select_questions(client: SupabaseREST, *, last_n: int, question_type: str,
                     tournament: str | None, since: str | None) -> list[tuple[dict, dict]]:
    """[(outcome, final run)] for the last ``last_n`` questions passing the filters.

    Forecasts are fetched a chunk of questions at a time, newest resolutions
    first, and only until enough questions qualify.
    """
    picked: list[tuple[dict, dict]] = []
    outcomes = resolved_outcomes(client, since)
    for start in range(0, len(outcomes), _ID_CHUNK):
        chunk = outcomes[start:start + _ID_CHUNK]
        ids = ",".join(str(o["question_id"]) for o in chunk)
        by_question: dict[int, list[dict]] = defaultdict(list)
        for row in _paged(client, "forecasts", {"select": _FORECAST_SELECT, "question_id": f"in.({ids})",
                                                "order": "question_id,run_id"}):
            by_question[row["question_id"]].append(row)
        for outcome in chunk:
            runs = by_question.get(outcome["question_id"])
            if not runs:
                continue
            run = final_run(runs)
            if question_type != "all" and _group_of(run.get("question_type")) != question_type:
                continue
            if _group_of(run.get("question_type")) is None or not matches_tournament(run, tournament):
                continue
            picked.append((outcome, run))
            if len(picked) >= last_n:
                return picked
    return picked


# ---------------------------------------------------------------------------
# Per-model forecasts and scores
# ---------------------------------------------------------------------------

def _grid_size(run: dict) -> int:
    final = run.get("final_forecast")
    if isinstance(final, list):
        return len(final)
    count = (run.get("scaling") or {}).get("inbound_outcome_count")
    return int(count) + 1 if count else 201


def model_forecasts(run: dict) -> list[tuple[str, str | None, Any]] | None:
    """[(model, backup_for, forecast)] for each usable run on this question, or
    None when the record cannot be lined up (run_values and the non-dropped
    ensemble records disagree in number). A backup run counts for the model
    that actually ran it. A numeric forecast is returned as its CDF."""
    kept = [r for r in (run.get("extra") or {}).get("ensemble") or []
            if isinstance(r, dict) and not r.get("dropped")]
    group = _group_of(run.get("question_type"))
    if group == "binary":
        out = [(r.get("model"), r.get("backup_for"), r["probability"] / 100) for r in kept
               if isinstance(r.get("probability"), (int, float))]
        return out if len(out) == len(kept) else None
    values = run.get("run_values") or []
    if len(values) != len(kept):
        return None
    if group == "multiple_choice":
        return [(r.get("model"), r.get("backup_for"), v) for r, v in zip(kept, values) if isinstance(v, dict)]
    scaling = run.get("scaling") or {}
    if scaling.get("range_min") is None or scaling.get("range_max") is None:
        return None
    grid = nominal_grid(float(scaling["range_min"]), float(scaling["range_max"]), _grid_size(run),
                        scaling.get("zero_point"))
    out = []
    for record, parsed in zip(kept, values):
        spec = parsed.get("spec") if isinstance(parsed, dict) else None
        if not spec:
            continue
        try:
            out.append((record.get("model"), record.get("backup_for"), spec_to_cdf(spec, grid)))
        except Exception:  # noqa: BLE001 - one unreadable run must not drop the question
            continue
    return out


def score_forecast(group: str, forecast: Any, resolution: str, scaling: dict) -> dict[str, float]:
    """{metric: score} for one forecast; empty when it cannot be scored."""
    if group == "binary":
        try:
            p = float(forecast)
        except (TypeError, ValueError):
            return {}
        brier = score_binary(p, resolution)
        if brier is None:
            return {}
        p_outcome = p if resolution == "yes" else 1.0 - p
        return {"brier": brier, "log": math.log(min(max(p_outcome, LOG_CLIP), 1 - LOG_CLIP))}
    if group == "multiple_choice":
        score = score_multiple_choice(forecast, resolution) if isinstance(forecast, dict) else None
        return {} if score is None else {"brier": score}
    score = crps_from_cdf(forecast, scaling, resolution)
    return {} if score is None else {"crps": score}


def _summarise(group: str, forecast: Any) -> str:
    if group == "binary":
        return f"{float(forecast):.3f}"
    if group == "multiple_choice":
        return "; ".join(f"{k}={float(v):.3f}" for k, v in forecast.items())
    return f"cdf[{len(forecast)}]"


def score_questions(selected: list[tuple[dict, dict]]) -> tuple[list[dict], Counter]:
    """One CSV row per (question, entity, metric), and counts of what was skipped."""
    rows, skipped = [], Counter()
    for outcome, run in selected:
        group = _group_of(run.get("question_type"))
        resolution = str(outcome["resolution"])
        scaling = run.get("scaling") or {}
        tournaments = "; ".join(str(t.get("name") or t.get("slug") or "")
                                for t in run.get("tournaments") or [] if isinstance(t, dict))
        base = {"question_id": run["question_id"], "post_id": run.get("post_id"), "title": run.get("title"),
                "group": group, "question_type": run.get("question_type"), "tournament": tournaments,
                "resolved_at": outcome.get("resolved_at"), "resolution": resolution, "run_id": run["run_id"]}
        entries: list[tuple[str, str | None, Any]] = []
        if run.get("final_forecast") is not None:
            entries.append((BOT, None, run["final_forecast"]))
        else:
            skipped["abstained (no submitted forecast)"] += 1
        per_model = model_forecasts(run)
        if per_model is None:
            skipped["runs could not be lined up with their models"] += 1
        else:
            entries.extend(per_model)
        for entity, backup_for, forecast in entries:
            for metric, score in score_forecast(group, forecast, resolution, scaling).items():
                rows.append({**base, "entity": entity or "unknown", "backup_for": backup_for or "",
                             "forecast": _summarise(group, forecast), "metric": metric, "score": score})
    return rows, skipped


# ---------------------------------------------------------------------------
# Statistics and report
# ---------------------------------------------------------------------------

def per_question_scores(rows: list[dict], group: str, metric: str) -> dict[str, dict[int, float]]:
    """entity -> {question_id: score}; a model that ran twice on one question
    (e.g. its own run and a backup run) gets the mean of the two."""
    collected: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["group"] == group and r["metric"] == metric:
            collected[r["entity"]][r["question_id"]].append(r["score"])
    return {e: {q: float(np.mean(v)) for q, v in qs.items()} for e, qs in collected.items()}


def bootstrap_ci(differences: list[float], resamples: int = BOOTSTRAP_RESAMPLES,
                 seed: int = 0) -> tuple[float, float]:
    d = np.asarray(differences, dtype=float)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, len(d), size=(resamples, len(d)))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def head_to_head(scores: dict[str, dict[int, float]], first: str, second: str,
                 lower_is_better: bool) -> dict | None:
    """``first``'s advantage over ``second`` on the questions both forecast:
    positive means ``first`` scored better."""
    shared = sorted(set(scores.get(first, {})) & set(scores.get(second, {})))
    if not shared:
        return None
    sign = 1.0 if lower_is_better else -1.0
    diffs = [sign * (scores[second][q] - scores[first][q]) for q in shared]
    low, high = bootstrap_ci(diffs) if len(diffs) > 1 else (diffs[0], diffs[0])
    if len(diffs) < 2:
        verdict = "one shared question: no interval"
    elif low > 0:
        verdict = "clearly better"
    elif high < 0:
        verdict = "clearly worse"
    else:
        verdict = "no clear difference"
    if len(diffs) < SMALL_N:
        verdict += " (small sample)"
    return {"n": len(diffs), "mean": float(np.mean(diffs)), "low": low, "high": high,
            "wins": sum(d > 1e-12 for d in diffs), "losses": sum(d < -1e-12 for d in diffs),
            "ties": sum(abs(d) <= 1e-12 for d in diffs), "verdict": verdict}


def _fmt(x: float) -> str:
    return f"{x:.4f}"


def build_summary(rows: list[dict], selected: list[tuple[dict, dict]], skipped: Counter, *,
                  filters: dict) -> str:
    lines = ["# Model scores", ""]
    dates = sorted(str(o.get("resolved_at") or "")[:10] for o, _ in selected if o.get("resolved_at"))
    shown = ", ".join(f"{k}={v}" for k, v in filters.items() if v not in (None, "", "all"))
    lines.append(f"**{len(selected)} resolved question(s)**"
                 + (f", resolved {dates[0]} to {dates[-1]}" if dates else "")
                 + (f"; filters: {shown}" if shown else "") + ".")
    by_group = Counter(_group_of(r.get("question_type")) for _, r in selected)
    lines.append("Questions by type: " + ", ".join(f"{GROUP_TITLES[g]} {n}" for g, n in by_group.items()) + ".")
    for reason, count in skipped.items():
        lines.append(f"- Skipped for per-model scoring: {count} question(s) -- {reason}.")
    backups = Counter(r["entity"] for r in rows if r["backup_for"] and r["metric"] in ("brier", "crps"))
    if backups:
        lines.append("- Backup runs, counted under the model that ran them: "
                     + ", ".join(f"`{m}` {n}" for m, n in backups.items()) + ".")
    lines.append("- The Metaculus community prediction is not included: the API returns no "
                 "aggregations to the bot's account.")
    lines.append("")
    for group in TYPE_GROUPS:
        group_metrics = [m for m in METRICS if any(r["group"] == group and r["metric"] == m for r in rows)]
        if not group_metrics:
            continue
        lines += [f"## {GROUP_TITLES[group]} ({by_group.get(group, 0)} questions)", ""]
        for metric in group_metrics:
            label, lower = METRICS[metric]
            scores = per_question_scores(rows, group, metric)
            order = sorted(scores, key=lambda e: np.mean(list(scores[e].values())) * (1 if lower else -1))
            lines += [f"### {label} ({'lower' if lower else 'higher'} is better)", "",
                      "| Model | Questions | Mean |", "|---|---:|---:|"]
            lines += [f"| `{e}` | {len(scores[e])} | {_fmt(float(np.mean(list(scores[e].values()))))} |"
                      for e in order]
            lines.append("")
            # Every pair, the higher-ranked model first. Its own questions decide
            # the ranking; only the shared ones decide the pair.
            pairs = [(a, b, head_to_head(scores, a, b, lower))
                     for i, a in enumerate(order) for b in order[i + 1:]]
            pairs = [p for p in pairs if p[2]]
            if not pairs:
                continue
            lines += ["Head-to-head, every pair, on the questions both forecast. Advantage > 0 means the "
                      "first model scored better; the 95% interval is a bootstrap over questions.", "",
                      "| First | Second | Shared | Advantage | 95% interval | First better / worse / tie | Verdict |",
                      "|---|---|---:|---:|---|---|---|"]
            for a, b, h in pairs:
                lines.append(f"| `{a}` | `{b}` | {h['n']} | {_fmt(h['mean'])} | [{_fmt(h['low'])}, "
                             f"{_fmt(h['high'])}] | {h['wins']} / {h['losses']} / {h['ties']} | {h['verdict']} |")
            lines.append("")
    return "\n".join(lines)


CSV_FIELDS = ["question_id", "post_id", "title", "question_type", "tournament", "resolved_at",
              "resolution", "run_id", "entity", "backup_for", "forecast", "metric", "score"]


def write_outputs(out_dir: str, summary: str, rows: list[dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")
    with open(os.path.join(out_dir, "per_forecast.csv"), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary + "\n")


def _date(value: str) -> str:
    return datetime.date.fromisoformat(value).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--last-n", type=int, default=DEFAULT_LAST_N)
    parser.add_argument("--question-type", choices=["all", *TYPE_GROUPS], default="all")
    parser.add_argument("--tournament", default="", help="substring of a tournament name or slug")
    parser.add_argument("--since", type=_date, default=None, help="only questions resolved on/after YYYY-MM-DD")
    parser.add_argument("--out", default="model-scores")
    args = parser.parse_args(argv)

    client = SupabaseREST.from_env()
    if client is None:
        print("[model-scores] SUPABASE_URL / SUPABASE_SERVICE_KEY not set.")
        return 1
    with client:
        selected = select_questions(client, last_n=args.last_n, question_type=args.question_type,
                                    tournament=args.tournament or None, since=args.since)
    rows, skipped = score_questions(selected)
    summary = build_summary(rows, selected, skipped,
                            filters={"type": args.question_type, "tournament": args.tournament,
                                     "since": args.since})
    write_outputs(args.out, summary, rows)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
