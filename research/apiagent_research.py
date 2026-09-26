"""API agent: an independent research step over apiagent-kit's public data APIs.

Given the question and a little context, a model with two tools (find_apis,
call_api) looks up whatever official data bears on it -- FRED, Treasury, FEC,
SEC, markets, ... -- and reports what it found. The result joins the other
research just before the artifact check and the brief.

Division of labour. The kit owns the tools: their schemas, the system prompt,
the per-turn data budget, the untrusted-data framing and the failure advice
(``apiagent-kit/cli.ts`` ops ``spec`` and ``tools``). This module owns the
loop and the model calls, so every call goes through the bot's cost ledger,
label routing and SoCLaaS streaming ladder. The loop mirrors
``apiagent-kit/src/agent/loop.ts``: tools are forced on the first two steps
(small models answer from memory otherwise), optional in the middle, and
removed on the last step so the turn always ends in an answer.

SoCLaaS Qwen only, by the owner's decision: this never runs on a paid model.
No SoCLaaS key, no Node, a failed call or no successful data retrieval all
mean "no section" -- never a failed question.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import apiagent_bridge
import llm_provider
import research_trace
import source_ledger
from llm_client import _create_chat_completion_with_retries
from monetary_cost_manager import HardLimitExceededError

logger = logging.getLogger(__name__)

SECTION_NAME = "Public Data APIs"
LABEL = "apiagent"
DEFAULT_MAX_STEPS = 6
# Question fields are context for choosing APIs, not the research itself.
_FIELD_CHARS = 1500


def _qwen_route() -> llm_provider.Route | None:
    """The pinned SoCLaaS Qwen route, or None when it cannot be used."""
    route = llm_provider.route_for_model(llm_provider.QWEN_RESEARCH_MODEL)
    if route.endpoint.label != "SoCLaaS" or not llm_provider.api_key_for(route.endpoint):
        return None
    return route


def _clip(text: str, limit: int = _FIELD_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def build_question_message(*, title: str, resolution_criteria: str, fine_print: str,
                           background: str, target_date: str, today: str) -> str:
    parts = [f"Forecasting question (today is {today}):", f"Title: {title.strip()}"]
    if resolution_criteria.strip():
        parts.append(f"Resolution criteria: {_clip(resolution_criteria)}")
    if fine_print.strip():
        parts.append(f"Fine print: {_clip(fine_print)}")
    if background.strip():
        parts.append(f"Background (excerpt): {_clip(background)}")
    if target_date:
        parts.append(f"Resolves on: {target_date[:10]}")
    parts.append(
        "\nFind the public data that bears on this question: the current value of "
        "whatever it resolves on, its recent history, official figures and their "
        "release schedule, and any prediction-market prices. Quote the numbers with "
        "their dates and the API each came from. Report data; do not forecast."
    )
    return "\n".join(parts)


def _validate(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "no choices"
    message = choices[0].message
    if (message.content or "").strip() or getattr(message, "tool_calls", None):
        return None
    return "empty answer and no tool call"


def _parse_args(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw  # the kit's schema check turns this into a readable error


def _call_line(call: dict[str, Any], output: dict[str, Any]) -> str | None:
    if call.get("name") != "call_api":
        return None
    api = output.get("api") or (call.get("args") or {}).get("api", "?")
    if output.get("error"):
        return f"- {api}: FAILED -- {str(output['error'])[:160]}"
    return (f"- {api} (Tier {output.get('tier', '?')}): {output.get('rowCount', 0)} rows"
            f"{' (truncated)' if output.get('truncated') else ''}, retrieved "
            f"{output.get('retrievedAt', '?')} -- {output.get('url', '')}")


def _raw_block(output: dict[str, Any], limit: int = 4000) -> str:
    rows = json.dumps(output.get("rows") or [], ensure_ascii=False)
    if len(rows) > limit:
        rows = rows[:limit] + " ... (cut)"
    return (f"### {output.get('api', '?')} (Tier {output.get('tier', '?')}), retrieved "
            f"{output.get('retrievedAt', '?')}\n{output.get('url', '')}\n"
            f"{output.get('note') or ''}\n```json\n{rows}\n```")


async def run_apiagent_research(
    *,
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    target_date: str = "",
    max_steps: int = DEFAULT_MAX_STEPS,
    today: dt.date | None = None,
) -> str | None:
    """Run the agent; return the ``Public Data APIs`` section text or None."""
    route = _qwen_route()
    if route is None:
        logger.info("[apiagent] skipped: SoCLaaS Qwen is not configured")
        return None
    usable, reason = apiagent_bridge.availability()
    if not usable:
        logger.info("[apiagent] skipped: %s", reason)
        return None

    today_iso = (today or dt.date.today()).isoformat()
    spec = await apiagent_bridge.agent_spec(today_iso)
    if not spec.get("ok"):
        logger.info("[apiagent] skipped: spec failed: %s", spec.get("error"))
        return None
    tools = [{"type": "function", "function": d} for d in spec["definitions"]]

    messages: list[dict[str, Any]] = [{"role": "user", "content": build_question_message(
        title=title, resolution_criteria=resolution_criteria, fine_print=fine_print,
        background=background, target_date=target_date, today=today_iso)}]
    chars_spent = 0
    call_lines: list[str] = []
    fetched = 0
    retrieved: list[dict[str, Any]] = []
    answer = ""
    max_steps = max(2, int(max_steps))

    async def ask(step: int, last: bool) -> Any:
        system = spec["lastStepPrompt"] if last else spec["systemPrompt"]
        payload: dict[str, Any] = {
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0.3,
            "stream": False,
        }
        if not last:  # the last step has no tools at all, so it must answer
            payload["tools"] = tools
            payload["tool_choice"] = "required" if step <= 1 else "auto"
        return await _create_chat_completion_with_retries(
            label=LABEL, model=route.model_id, route=route,
            request_payload=payload, validate_response=_validate)

    for step in range(max_steps):
        last = step == max_steps - 1
        try:
            response = await ask(step, last)
        except HardLimitExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad step must not lose the data
            # 45412, 2026-09-26: a mid-loop step came back empty and the whole
            # agent was lost with the Treasury rows it had already fetched.
            # Ask once more, tools removed, for an answer from what is in hand.
            logger.warning("[apiagent] step %d failed (%s); asking for a final answer",
                           step, exc)
            if fetched and not last:
                try:
                    response = await ask(step, True)
                except HardLimitExceededError:
                    raise
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning("[apiagent] final answer failed too: %s", retry_exc)
                    break
            else:
                break
        message = response.choices[0].message
        text = (message.content or "").strip()
        answer = text or answer
        calls = [{"id": c.id, "name": c.function.name,
                  "args": _parse_args(c.function.arguments)}
                 for c in (getattr(message, "tool_calls", None) or [])]
        if not calls:
            break

        messages.append({"role": "assistant", "content": text or None, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
            for c in calls]})
        step_result = await apiagent_bridge.dispatch_tools(calls, chars_spent)
        if not step_result.get("ok"):
            logger.warning("[apiagent] step %d tools failed: %s", step, step_result.get("error"))
            break
        chars_spent += int(step_result.get("charsReturned") or 0)
        outputs = {r.get("id"): r.get("output") or {} for r in step_result.get("results") or []}
        for call in calls:
            output = outputs.get(call["id"], {"error": "no result returned"})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(output, ensure_ascii=False)})
            line = _call_line(call, output)
            if line:
                call_lines.append(line)
            if call["name"] == "call_api" and not output.get("error"):
                fetched += 1
                retrieved.append(output)
                if output.get("url"):
                    source_ledger.record_url_event(
                        output["url"], source_ledger.ROLE_SCRAPED,
                        engine=f"apiagent:{output.get('api', '')}", ok=True,
                        chars=len(json.dumps(output.get("rows") or [])),
                        round_label="single pass")
        research_trace.emit(
            "apiagent", f"step {step}",
            {"calls": calls, "outputs": outputs, "chars_spent": chars_spent},
            meta={"step": step, "calls": len(calls)})

    if not fetched:
        logger.info("[apiagent] no section: no successful data call (answer=%d chars)",
                    len(answer))
        return None
    if not answer:
        # The model never wrote an answer, but the data is real: hand the
        # compiler the retrieved rows themselves rather than nothing.
        answer = "The agent retrieved data but wrote no summary. Raw results:\n\n" + \
            "\n\n".join(_raw_block(output) for output in retrieved)
    return (
        "_Gathered by an API agent from public data APIs during this run. Tier A "
        "sources are official/resolution-grade; Tier B are inputs only._\n\n"
        f"{answer}\n\n## Calls made\n" + "\n".join(call_lines)
    )
