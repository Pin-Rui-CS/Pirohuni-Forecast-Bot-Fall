"""The shape every check returns."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PASS, WARN, FAIL, INFO, SKIPPED = "pass", "warn", "fail", "info", "skipped"
_ORDER = {FAIL: 0, WARN: 1, INFO: 2, PASS: 3, SKIPPED: 4}


@dataclass
class Check:
    check_id: str          # stable, e.g. "pipeline.search_chain"
    category: str          # pipeline | sources | condensation | evidence | forecast
    title: str
    status: str            # pass | warn | fail | info | skipped
    detail: str = ""
    value: Any = None      # machine-readable result (stored as jsonb)
    evidence: list[str] = field(default_factory=list)
    method: str = "code"   # code | qwen

    def as_row(self) -> dict:
        return asdict(self)


def sort_key(check: Check) -> tuple:
    return (_ORDER.get(check.status, 9), check.category, check.check_id)


def skipped(check_id: str, category: str, title: str, why: str, method: str = "code") -> Check:
    return Check(check_id, category, title, SKIPPED, why, method=method)
