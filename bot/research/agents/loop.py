"""The turn loop of a research agent: the model replies with one JSON action
per turn, the agent's handler for that action returns a text result, and the
loop ends on the "final" action, the turn limit or the wall clock.

An agent supplies its own system prompt, action handlers, limits and trace
names (see source_explorer and history_builder). Runs only on SoCLaaS (the
SOCLAAS_MODEL_CHAIN, DeepSeek first): there is never a paid fallback.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Awaitable, Callable

from bot.config import llm_rate_limiter
from bot.llm import provider as llm_provider
from bot.llm import qwen_ladder, soclaas_models
from bot.llm.cost_manager import MonetaryCostManager
from bot.trace import research_trace

logger = logging.getLogger(__name__)

# The whole conversation stays inside the smallest context in the SoCLaaS
# chain (Qwen is the fallback). Older tool results are cut first.
MAX_CONVERSATION_CHARS = 300_000
TRIMMED_RESULT_CHARS = 3_000

Handler = Callable[[dict], Awaitable[str]]


@dataclass
class Outcome:
    """How a run ended. ``final`` is the model's final action, or None when the
    run stopped first (``stopped`` says why)."""

    final: dict | None = None
    turns: int = 0
    stopped: str = ""
    actions: list[str] = field(default_factory=list)


def soclaas_route() -> llm_provider.Route:
    return llm_provider.route_for_model(soclaas_models.SOCLAAS_ROLE)


def parse_action(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    return value


def int_arg(action: dict, name: str, default: int = 0) -> int:
    try:
        return int(action.get(name) or default)
    except (TypeError, ValueError):
        return default


def trim(messages: list[dict]) -> None:
    """Cut the oldest tool results until the conversation fits."""
    total = sum(len(m["content"]) for m in messages)
    for message in messages[2:-1]:
        if total <= MAX_CONVERSATION_CHARS:
            return
        if message["role"] == "user" and len(message["content"]) > TRIMMED_RESULT_CHARS:
            total -= len(message["content"]) - TRIMMED_RESULT_CHARS
            message["content"] = (message["content"][:TRIMMED_RESULT_CHARS]
                                  + "\n[older tool result trimmed to save space]")


async def ask(route: llm_provider.Route, messages: list[dict], label: str) -> Any:
    payload = {"model": route.model_id, "messages": messages, "max_tokens": 32000}
    await llm_provider.gate_for(route).wait_async()
    async with llm_rate_limiter:
        handle = MonetaryCostManager.start_openrouter_call(label, route.model_id, payload)
        try:
            response = await qwen_ladder.run_async(route, payload)
        except BaseException as exc:
            handle.record_failure(exc)
            raise
        handle.record_response(response)
    return response


async def run(*, system: str, handlers: dict[str, Handler], label: str, trace_stage: str,
              trace_name: str, max_turns: int, wall_seconds: float,
              meta: Callable[[], dict] | None = None,
              status_line: Callable[[], str] | None = None) -> Outcome:
    """Run the loop. ``handlers`` maps an action name to its coroutine; "final"
    needs none. ``meta()`` adds fields to each turn's trace record, and
    ``status_line()`` a line appended to every tool result (budgets left).

    A turn limit, the wall clock or a SoCLaaS failure ends the run with
    ``final`` None; the caller decides what it can still report. A hard cost
    limit (HardLimitExceededError) is never swallowed.
    """
    route = soclaas_route()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Begin. Reply with your first JSON action."}]
    names = ", ".join([*handlers, "final"])
    outcome = Outcome()
    started = time.monotonic()
    try:
        async with asyncio.timeout(wall_seconds):
            for turn in range(1, max_turns + 1):
                response = await ask(route, messages, label)
                reply = response.choices[0].message.content or ""
                messages.append({"role": "assistant", "content": reply})
                outcome.turns = turn
                final = None
                try:
                    action = parse_action(reply)
                except ValueError as exc:
                    action = {"action": "invalid"}
                    result = f"Could not read your reply ({exc}). Reply with one JSON object."
                else:
                    kind = action.get("action")
                    if kind == "final":
                        final, result = action, ""
                    elif kind in handlers:
                        result = await handlers[kind](action)
                    else:
                        result = f"Unknown action {kind!r}; use one of: {names}."
                outcome.actions.append(str(action.get("action")))
                if final is None:
                    if status_line is not None:
                        result += "\n\n" + status_line()
                    if turn == max_turns - 1:
                        result += "\n\nOne turn left: your next reply must be the final action."
                research_trace.emit(
                    trace_stage, f"{trace_name} turn {turn} ({action.get('action')})",
                    reply + (f"\n\n--- tool result ({len(result):,} chars) ---\n{result}" if result else ""),
                    meta={"turn": turn, "model": getattr(response, "soclaas_model", route.model_id),
                          **(meta() if meta is not None else {})},
                    kind="model")
                logger.info("[%s] turn %d: %s -> %d chars", label, turn, action.get("action"), len(result))
                if final is not None:
                    outcome.final = final
                    return outcome
                messages.append({"role": "user", "content": result})
                trim(messages)
            outcome.stopped = f"the {max_turns}-turn limit"
    except (TimeoutError, qwen_ladder.QwenLadderFailed) as exc:
        outcome.stopped = f"{type(exc).__name__}: {exc}".rstrip(": ")
        logger.warning("[%s] stopped after %.0fs: %s", label, time.monotonic() - started, outcome.stopped)
    return outcome
