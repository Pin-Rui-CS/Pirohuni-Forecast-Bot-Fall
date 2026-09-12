from __future__ import annotations

import asyncio
import json
import os
import logging
from collections.abc import Callable
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

import llm_provider
from config import llm_rate_limiter
from monetary_cost_manager import MonetaryCostManager, count_openrouter_reasoning_tokens
from utils import _get_field, _json_default, _truncate_text

logger = logging.getLogger(__name__)

OPENROUTER_MAX_ATTEMPTS = max(1, int(os.getenv("OPENROUTER_MAX_ATTEMPTS", "3")))
OPENROUTER_RETRY_BASE_SECONDS = float(os.getenv("OPENROUTER_RETRY_BASE_SECONDS", "2.0"))

# A validator may prefix its problem string with this to mean "do not retry":
# the failure is deterministic and a retry would only burn the same tokens
# again. Used for the reasoning-ate-the-output-cap case, where three attempts
# at a 34k-token cap is an expensive way to fail identically.
FATAL_PROBLEM_PREFIX = "FATAL: "


class ResponseTruncatedError(RuntimeError):
    """A completion that stopped at the output cap with content already emitted.

    An EMPTY response with finish_reason=length is already fatal upstream (see
    ``_empty_content_problem``). The dangerous case is the *partial* one: the
    text looks well-formed, so a caller that treats it as a complete answer
    silently loses whatever came after the cut. Callers that cannot tolerate a
    partial answer pass ``raise_on_truncation=True``.
    """


class RetryableLLMResponseError(RuntimeError):
    """Raised when the provider returns an empty or malformed completion."""


async def _create_chat_completion_with_retries(
    *,
    label: str,
    model: str,
    request_payload: dict[str, Any],
    validate_response: Callable[[Any], str | None],
    route: Any = None,
) -> Any:
    route = route or llm_provider.route_for(model, label=label)
    client = llm_provider.async_client_for(route)
    gate = llm_provider.gate_for(route)
    provider = route.endpoint.label
    last_problem = f"{provider} request did not run"
    for attempt in range(1, OPENROUTER_MAX_ATTEMPTS + 1):
        retry_after_exception = False
        # Outside the semaphore: sleeping for a rate slot while holding one of
        # the five global concurrency slots would starve every other endpoint.
        # Inside the retry loop: a retry is another request against the limit,
        # and a limiter that does not charge for retries is a rate amplifier.
        await gate.wait_async()
        async with llm_rate_limiter:
            usage_handle = MonetaryCostManager.start_openrouter_call(
                label,
                model,
                request_payload,
            )
            try:
                response = await client.chat.completions.create(
                    **llm_provider.build_kwargs(route, request_payload)
                )
            except (APIConnectionError, APIStatusError, APITimeoutError, RateLimitError) as exc:
                llm_provider.note_failure(route, exc)
                problem = _format_openrouter_exception(exc)
                usage_handle.record_output(problem)
                last_problem = problem
                logger.warning(
                    "[%s] %s attempt %d/%d failed: %s",
                    provider,
                    label,
                    attempt,
                    OPENROUTER_MAX_ATTEMPTS,
                    problem,
                )
                if attempt >= OPENROUTER_MAX_ATTEMPTS or not _is_retryable_openrouter_exception(exc):
                    raise RuntimeError(
                        f"{provider} request failed for {label}: {problem}"
                    ) from exc
                retry_after_exception = True

        if retry_after_exception:
            await asyncio.sleep(_retry_delay_seconds(attempt))
            continue

        usage_handle.record_response(response)
        problem = validate_response(response)
        if problem is None:
            if attempt > 1:
                logger.info("[%s] %s recovered on attempt %d.", provider, label, attempt)
            return response

        last_problem = problem
        logger.warning(
            "[%s] %s attempt %d/%d returned unusable response: %s\n%s",
            provider,
            label,
            attempt,
            OPENROUTER_MAX_ATTEMPTS,
            problem,
            _describe_openrouter_response(response),
        )
        if problem.startswith(FATAL_PROBLEM_PREFIX):
            break
        if attempt < OPENROUTER_MAX_ATTEMPTS:
            await asyncio.sleep(_retry_delay_seconds(attempt))

    raise RetryableLLMResponseError(
        f"{provider} returned unusable response for {label} after "
        f"{OPENROUTER_MAX_ATTEMPTS} attempt(s): {last_problem}"
    )


def _retry_delay_seconds(attempt: int) -> float:
    return OPENROUTER_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))


def _is_retryable_openrouter_exception(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code in {408, 409, 429, 500, 502, 503, 504}


def _format_openrouter_exception(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    response_text = getattr(response, "text", None)
    pieces = [exc.__class__.__name__]
    if status_code is not None:
        pieces.append(f"status={status_code}")
    pieces.append(str(exc))
    if response_text:
        pieces.append(f"body={_truncate_text(response_text, 1000, '... [truncated]')}")
    return " | ".join(pieces)


def _validate_text_completion_response(response: Any) -> str | None:
    problem = _validate_common_openrouter_response(response)
    if problem:
        return problem
    choice = _get_field(response, "choices")[0]
    message = _get_field(choice, "message")
    content = _get_field(message, "content")
    if content is None or not str(content).strip():
        return _empty_content_problem(choice, response)
    return None


def _empty_content_problem(choice: Any, response: Any) -> str:
    """Describe an empty assistant message, flagging the reasoning-cap case.

    On a reasoning model the completion cap covers internal reasoning as well
    as the visible answer, so a cap sized for the answer alone can be spent
    entirely on reasoning: finish_reason=length with zero content. Retrying
    reproduces it exactly and bills the same tokens again, so mark it fatal
    and name the fix.
    """
    if str(_get_field(choice, "finish_reason") or "").lower() == "length":
        reasoning = count_openrouter_reasoning_tokens(response)
        return (
            f"{FATAL_PROBLEM_PREFIX}assistant content is empty and "
            f"finish_reason=length -- the completion cap was consumed by "
            f"{reasoning} reasoning tokens before any visible output. Raise "
            f"max_tokens for this call or lower its reasoning effort "
            f"(see REASONING_HEADROOM_TOKENS in llm_provider.py)."
        )
    return "assistant message content is empty"


def _validate_common_openrouter_response(response: Any) -> str | None:
    response_error = _extract_openrouter_error(response)
    if response_error:
        return f"{llm_provider.provider_name()}/provider error: {response_error}"

    choices = _get_field(response, "choices")
    if not isinstance(choices, list) or not choices:
        return f"choices is {type(choices).__name__ if choices is not None else 'None'}"

    choice = choices[0]
    finish_reason = _get_field(choice, "finish_reason")
    if finish_reason == "error":
        return f"finish_reason=error: {_format_value(_get_field(choice, 'error'))}"

    if _get_field(choice, "message") is None:
        return "choice has no message"

    return None


def _extract_openrouter_error(response: Any) -> str | None:
    top_level_error = _get_field(response, "error")
    if top_level_error:
        return _format_value(top_level_error)

    choices = _get_field(response, "choices")
    if isinstance(choices, list):
        for index, choice in enumerate(choices):
            choice_error = _get_field(choice, "error")
            if choice_error:
                return f"choice[{index}].error={_format_value(choice_error)}"
    return None


def _describe_openrouter_response(response: Any) -> str:
    lines = [
        "[OpenRouter diagnostic]",
        f"response_id={_get_field(response, 'id')!r}",
        f"model={_get_field(response, 'model')!r}",
        f"provider={_get_field(response, 'provider')!r}",
        f"usage={_format_value(_get_field(response, 'usage'))}",
        f"top_level_error={_format_value(_get_field(response, 'error'))}",
    ]
    choices = _get_field(response, "choices")
    if not isinstance(choices, list):
        lines.append(f"choices={_format_value(choices)}")
        return "\n".join(lines)

    lines.append(f"choices_count={len(choices)}")
    for index, choice in enumerate(choices[:3]):
        message = _get_field(choice, "message")
        content = _get_field(message, "content")
        tool_calls = _get_field(message, "tool_calls")
        lines.append(
            "choice[{index}]: finish_reason={finish!r}, native_finish_reason={native!r}, "
            "content_chars={chars}, tool_calls={tool_count}, error={error}".format(
                index=index,
                finish=_get_field(choice, "finish_reason"),
                native=_get_field(choice, "native_finish_reason"),
                chars=len(content) if isinstance(content, str) else 0,
                tool_count=len(tool_calls) if isinstance(tool_calls, list) else 0,
                error=_format_value(_get_field(choice, "error")),
            )
        )
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if value is None:
        return "None"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
    except (TypeError, ValueError):
        text = str(value)
    return _truncate_text(text, 1000, "... [truncated]")


def _user_message(prompt: str, cache_static_prefix: bool, route: Any) -> dict:
    """Build the user message, optionally marking the prompt as a cacheable
    prefix (OpenRouter forwards cache_control to providers that support
    prompt caching, e.g. Anthropic; others ignore it).

    The OpenAI API has no message-body equivalent -- it caches automatically
    and is steered by ``prompt_cache_options`` instead -- so the breakpoint is
    dropped there rather than sent as an unrecognised content-part field.
    """
    if not cache_static_prefix or not route.supports_cache_control:
        return {"role": "user", "content": prompt}
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


async def call_llm(
    prompt: str,
    model: str = "anthropic/claude-opus-5",
    temperature: float = 0.3,
    _label: str = "forecast",
    return_transcript: bool = False,
    cache_static_prefix: bool = False,
    max_tokens: int | None = None,
    raise_on_truncation: bool = False,
) -> str | tuple[str, str]:
    """Call the LLM via the provider selected by ``LLM_PROVIDER``.

    The returned transcript intentionally omits the user prompt; callers that
    save transcripts store the prompt once themselves. ``max_tokens`` caps the
    response length when set (used by bounded utility passes like the
    compiler's pre-compression); when None the provider default applies. Under
    OpenAI the cap is grown to cover reasoning tokens -- see
    ``llm_provider.max_completion_tokens_for``.

    ``model`` is given in the bot's internal namespace (e.g.
    ``anthropic/claude-opus-5``, which names the *role* as much as the model)
    and is translated to the active provider's namespace here, so logs, the
    transcript, and the usage ledger all record what actually ran.
    """
    route = llm_provider.route_for(model, label=_label)
    model = route.model_id
    transcript_parts = [
        "# LLM Transcript",
        f"Label: {_label}",
        f"Model: {model}",
    ]

    def finish(answer: str) -> str | tuple[str, str]:
        if not return_transcript:
            return answer
        return answer, "\n\n".join(transcript_parts)

    logger.info("[LLM] %s | model=%s | prompt_chars=%d", _label, model, len(prompt))
    logger.debug("[LLM] %s prompt:\n%s", _label, prompt)

    messages = [_user_message(prompt, cache_static_prefix, route)]
    request_payload: dict = {
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens is not None:
        request_payload["max_tokens"] = max_tokens
    response = await _create_chat_completion_with_retries(
        label=_label,
        model=model,
        route=route,
        request_payload=request_payload,
        validate_response=_validate_text_completion_response,
    )
    choice = response.choices[0]
    answer = choice.message.content
    logger.debug("[LLM] %s response:\n%s", _label, answer)
    if answer is None:
        raise ValueError("No answer returned from LLM")
    # A response cut off at the output cap is non-empty and well-formed, so
    # nothing downstream can tell it from a complete answer. An EMPTY response
    # with finish_reason=length is already fatal upstream (_empty_content_problem);
    # this is the partial case. Always say so; raise only for callers whose
    # output is meaningless when partial.
    if str(getattr(choice, "finish_reason", "") or "").lower() == "length":
        logger.warning(
            "[LLM] %s | response TRUNCATED at the output cap (max_tokens=%s, %d chars). "
            "Raise the cap or shrink the input.",
            _label, max_tokens, len(answer),
        )
        if raise_on_truncation:
            raise ResponseTruncatedError(
                f"{_label}: finish_reason=length at max_tokens={max_tokens} "
                f"after {len(answer)} chars"
            )
    transcript_parts += [
        "## Assistant Final Response",
        answer,
    ]
    return finish(answer)
