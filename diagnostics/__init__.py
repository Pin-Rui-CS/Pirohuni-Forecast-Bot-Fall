"""Run diagnostics: an objective checklist over one forecast's saved artifacts.

    python eval_tools/diagnose_run.py local <question_dir>
    python eval_tools/diagnose_run.py fetch --question-id 46133

Reads only what a run saved (forecast.json, research.md, runs.md, audit.md,
trace/, run.log, precompression folders) -- it never forecasts and never calls a
paid model. Code answers everything it can; Qwen (SoCLaaS) is used only for
narrow extraction tasks whose answers code then verifies (qwen_tasks.py).
"""
