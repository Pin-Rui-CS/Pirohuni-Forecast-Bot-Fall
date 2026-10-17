"""What the current task is tracing: the active question's trace directory and
the research provider scope running in it. Shared by research_trace (which
sets them) and call_log (which tags each external call with them), so neither
module imports the other."""
from __future__ import annotations

from contextvars import ContextVar

ACTIVE: ContextVar[str] = ContextVar("research_trace_dir", default="")
# The research provider whose work is running here (see research_trace.scope):
# its name is the lane, its feeds are what its first steps read, and it collects
# the references its steps write so the provider's section can say what it joined.
SCOPE: ContextVar[dict | None] = ContextVar("research_trace_scope", default=None)
