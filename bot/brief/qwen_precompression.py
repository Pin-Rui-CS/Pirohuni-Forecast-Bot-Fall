"""Opt-in precompression on SoCLaaS. Only the SoCLaaS endpoint is reachable.

Uses the models in SOCLAAS_MODEL_CHAIN (DeepSeek, then Qwen by default); the
module name predates DeepSeek.

Saved originals and attempts are mandatory. Mechanical checks are conservative
failure detectors, not a semantic quality certificate. No paid recovery exists.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Callable
from uuid import uuid4

import httpx

from bot.llm import provider as llm_provider
from bot.llm import qwen_ladder
from bot.llm import soclaas_models
from bot.config import llm_rate_limiter
from bot.llm.cost_manager import MonetaryCostManager
from bot.paths import REPO_ROOT

ROOT = REPO_ROOT
# HTTP statuses that mean "this model is gone or refuses these settings" (a
# retired test model, an effort it does not accept): the next model may work.
MODEL_REJECTED_STATUS = qwen_ladder.MODEL_REJECTED_STATUS
URL = re.compile(r"https?://[^\s<>\[\]\"()]+")
NUMBER = re.compile(r"(?<![\w])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w])")


class PrecompressionPaused(RuntimeError):
    """Experimental precompression must not continue to lossy or paid recovery."""


class AttemptRejected(RuntimeError):
    pass


# The gateway would not serve this model; move to the next in the chain.
ModelRejected = qwen_ladder.ModelRejected


@dataclass(frozen=True)
class Candidate:
    answer: str | None
    artifact_dir: Path
    selected_effort: str | None
    selected_model: str | None = None


def endpoint_url() -> str:
    """The SoCLaaS chat URL, honouring SOCLAAS_BASE_URL like every other call."""
    return llm_provider.ENDPOINT_SOCLAAS.base_url.rstrip("/") + "/chat/completions"


def mode() -> str:
    value = os.getenv("SOCLAAS_PRECOMPRESS_MODE", "off").strip().lower()
    if value not in {"off", "review", "experimental"}:
        raise ValueError("SOCLAAS_PRECOMPRESS_MODE must be off, review or experimental")
    return value


def checks_mode() -> str:
    """strict: failed mechanical checks send the attempt down the ladder.
    advisory: they are recorded but only a transport/completion failure does.

    The mechanical checks flag every number or URL that did not survive,
    including boilerplate, so strict mode rejected both completed answers in
    the 2026-09-21 replay. Advisory mode is for a whole-pipeline trial that
    judges the output by manual review instead.
    """
    value = os.getenv("SOCLAAS_PRECOMPRESS_CHECKS", "").strip().lower() or "strict"
    if value not in {"strict", "advisory"}:
        raise ValueError("SOCLAAS_PRECOMPRESS_CHECKS must be strict or advisory")
    return value


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def mechanical_checks(source: str, answer: str) -> dict:
    """Value/URL checks; normalize cosmetic number formatting, not meaning."""
    def urls(text):
        return {x.rstrip(".,;:") for x in URL.findall(text)}
    def numbers(text):
        values = set()
        for token in NUMBER.findall(URL.sub("", text)):
            percent = "%" if token.endswith("%") else ""
            raw = token.rstrip("%").replace(",", "")
            sign = "-" if raw.startswith("-") else ""
            integer, _, fraction = raw.lstrip("+-").partition(".")
            integer = integer.lstrip("0") or "0"
            fraction = fraction.rstrip("0")
            if integer == "0" and not fraction:
                sign = ""
            values.add(sign + integer + ("." + fraction if fraction else "") + percent)
        return values
    source_urls, answer_urls = urls(source), urls(answer)
    source_numbers, answer_numbers = numbers(source), numbers(answer)
    return {
        "missing_urls": sorted(source_urls - answer_urls),
        "added_urls": sorted(answer_urls - source_urls),
        "missing_number_tokens": sorted(source_numbers - answer_numbers),
        "added_number_tokens": sorted(answer_numbers - source_numbers),
        "empty": not answer.strip(),
        "semantic_fidelity": "not assessed; exact checks can have false positives and false negatives",
    }


def problems(checks: dict) -> list[str]:
    return [key for key in ("missing_urls", "added_urls", "missing_number_tokens",
                           "added_number_tokens", "empty") if checks[key]]


async def generate_candidate(
    *, name: str, source: str, messages: list[dict], artifact_dir: Path | None = None,
    timeout_seconds: float = 600, max_tokens: int = 65536,
    transport: httpx.AsyncBaseTransport | None = None,
    evidence_check: Callable[[str, str], list[str]] | None = None,
) -> Candidate:
    """Each SoCLaaS model in the chain, each of its efforts once, from identical inputs.

    Models come from SOCLAAS_MODEL_CHAIN (DeepSeek, then Qwen by default) and
    each model's efforts from soclaas_models (DeepSeek high then low, Qwen
    XHigh then Medium). A model the gateway rejects (HTTP 400/404, or a reply
    naming another model) is skipped for the next one; a spent quota or an
    access refusal pauses instead, since another model on the same allowance
    would not cure it.

    ``evidence_check`` may add source-grounded rejection reasons. It is optional
    because semantic review is manual in the initial replay. Never automatically
    resends an existing artifact directory, including an interrupted attempt.
    """
    if timeout_seconds <= 0 or not 1 <= max_tokens <= 65536:
        raise ValueError("Invalid timeout or combined reasoning/output allowance")
    head = llm_provider.route_for_model(soclaas_models.SOCLAAS_ROLE)
    if head.endpoint.name != llm_provider.ENDPOINT_SOCLAAS.name:
        raise ValueError("Precompression must use the SoCLaaS endpoint")
    models = soclaas_models.fallbacks_from(head.model_id)
    endpoint = endpoint_url()
    key = llm_provider.api_key_for(head.endpoint)
    if not key:
        raise PrecompressionPaused("SOCLAAS_API_KEY is not configured")
    try:
        qwen_ladder._raise_if_down("compiler/precompress")
    except qwen_ladder.QwenUnavailable as exc:
        raise PrecompressionPaused(str(exc)) from exc
    if artifact_dir is None:
        artifact_dir = ROOT / "data/qwen-precompression" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10])
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "original.md").write_text(source, encoding="utf-8")
    save_json(artifact_dir / "messages.json", messages)
    manifest = {
        "name": name, "models": list(models), "endpoint": endpoint, "source_sha256": digest(source),
        "messages_sha256": digest(json.dumps(messages, ensure_ascii=False, sort_keys=True)),
        "attempts": [], "selected_model": None, "selected_effort": None, "status": "running",
        "semantic_review": "pending", "paid_calls": 0,
    }
    save_json(artifact_dir / "manifest.json", manifest)

    async def restrict(request):
        if request.method != "POST" or str(request.url) != endpoint:
            raise ValueError("Only the SoCLaaS chat endpoint is allowed")

    try:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + key}, follow_redirects=False,
            timeout=httpx.Timeout(connect=25, read=timeout_seconds, write=45, pool=30),
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            event_hooks={"request": [restrict]},
        ) as client:
            for model_id in models:
                model = soclaas_models.get(model_id)
                route = llm_provider.route_for_model(model_id)
                efforts = model.efforts
                skip_model = False
                for rung, effort in enumerate(efforts):
                    attempt_name = f"{model_id.replace(':', '_')}-{effort}"
                    attempt_dir = artifact_dir / attempt_name
                    attempt_dir.mkdir()
                    payload = {"model": model_id, "messages": messages, "stream": True,
                               "stream_options": {"include_usage": True},
                               "reasoning_effort": effort, **model.sampler, "seed": 19019,
                               "max_tokens": max_tokens}
                    save_json(attempt_dir / "request.json", payload)
                    record = {"model": model_id, "effort": effort, "status": "reserved", "paid_calls": 0,
                              "timeout_seconds": timeout_seconds, "max_tokens": max_tokens,
                              "started_utc": datetime.now(timezone.utc).isoformat()}
                    save_json(attempt_dir / "result.json", record)
                    manifest["attempts"].append(attempt_name)
                    save_json(artifact_dir / "manifest.json", manifest)
                    state = qwen_ladder.StreamState()
                    start = time.monotonic()
                    handle = None
                    unavailable = False
                    try:
                        label = f"compiler/precompress/{model_id}/{effort}"
                        accounting_route = llm_provider.route_for(model_id, label=label)
                        if accounting_route.endpoint != route.endpoint or accounting_route.model_id != model_id:
                            raise PrecompressionPaused("Task override conflicts with the SoCLaaS accounting route")
                        await llm_provider.gate_for(route).wait_async()
                        # One of the endpoint's max_in_flight stream slots, shared
                        # with every ladder call (2026-10-05: six open streams).
                        async with llm_provider.gate_for(route).in_flight_async(), llm_rate_limiter:
                            start = time.monotonic()
                            handle = MonetaryCostManager.start_openrouter_call(label, model_id, payload)
                            async with asyncio.timeout(timeout_seconds):
                                async with client.stream("POST", endpoint, json=payload) as response:
                                    record["http_status"] = response.status_code
                                    if response.status_code >= 400:
                                        # Keep the body: a burst limit and a spent
                                        # quota are both 429 and need different fixes.
                                        await response.aread()
                                        record["error_body"] = response.text[:1000]
                                    response.raise_for_status()
                                    async for line in response.aiter_lines():
                                        raw = line[5:].strip() if line.startswith("data:") else ""
                                        if not raw:
                                            continue
                                        try:
                                            qwen_ladder.consume(state, raw, model_id)
                                        except ModelRejected:
                                            raise  # the gateway served something else: try the next model
                                        except qwen_ladder.StreamRefused as exc:
                                            # In-band quota/access errors are not cured by less thinking.
                                            raise PrecompressionPaused(str(exc)) from exc
                                        except qwen_ladder.AttemptFailed as exc:
                                            raise AttemptRejected(str(exc)) from exc
                                        finally:
                                            if state.model:
                                                record["response_model"] = state.model
                                        if state.done:
                                            break
                        try:
                            qwen_ladder.check_complete(state)
                        except qwen_ladder.AttemptFailed as exc:
                            raise AttemptRejected(str(exc)) from exc
                        checks = mechanical_checks(source, state.answer)
                        record["checks"] = checks
                        rejected = problems(checks)
                        if evidence_check:
                            rejected += evidence_check(source, state.answer)
                        record["rejection_reasons"] = rejected
                        if rejected and checks_mode() == "strict":
                            raise AttemptRejected("Evidence checks rejected answer: " + ", ".join(rejected))
                        record["status"] = "candidate"
                    except httpx.HTTPStatusError as exc:
                        record.update(status="failed", error_type=type(exc).__name__)
                        llm_provider.note_failure(route, exc)
                        status = exc.response.status_code
                        # A busy-gateway 429 ("dispatch queue wait expired") is
                        # transient like a 5xx (2026-10-05, question 46056).
                        if status in qwen_ladder.RETRY_STATUS or qwen_ladder.is_overload_429(status, exc.response):
                            unavailable = True
                        elif status in MODEL_REJECTED_STATUS:
                            # This model is gone or refuses these settings; the next may not.
                            skip_model = True
                        else:
                            # A spent quota or an access refusal: no other model cures it.
                            raise PrecompressionPaused(f"SoCLaaS HTTP {status}; no thinking retry") from exc
                    except ModelRejected as exc:
                        record.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                        skip_model = True
                    except (TimeoutError, httpx.TransportError, AttemptRejected, json.JSONDecodeError) as exc:
                        record.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                    except BaseException as exc:
                        record.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "blocked",
                                      error_type=type(exc).__name__)
                        raise
                    finally:
                        answer, reasoning = state.answer, state.reasoning
                        record.update(seconds=round(time.monotonic() - start, 3), finish_reason=state.finish,
                                      done_marker=state.done, usage=state.usage, answer_chars=len(answer),
                                      reasoning_chars=len(reasoning), answer_sha256=digest(answer))
                        (attempt_dir / "answer.md").write_text(answer, encoding="utf-8")
                        (attempt_dir / "reasoning.txt").write_text(reasoning, encoding="utf-8")
                        save_json(attempt_dir / "result.json", record)
                        if handle:
                            handle.record_response({"model": model_id, "usage": state.usage or {}, "choices": [{
                                "message": {"content": answer, "reasoning": reasoning},
                                "finish_reason": state.finish}]})
                    if record["status"] == "candidate":
                        manifest.update(status="candidate_pending_semantic_review", selected_model=model_id,
                                        selected_effort=effort,
                                        advisory_rejection_reasons=record["rejection_reasons"])
                        save_json(artifact_dir / "manifest.json", manifest)
                        return Candidate(state.answer, artifact_dir, effort, model_id)
                    if skip_model:
                        break
                    if unavailable and rung < len(efforts) - 1 and qwen_ladder.UNAVAILABLE_BACKOFF_SECONDS:
                        # Same shared pause the ladder uses before its one retry.
                        await qwen_ladder._wait_unavailable_async(
                            route, qwen_ladder.UNAVAILABLE_BACKOFF_SECONDS[0])
        manifest["status"] = "no_acceptable_candidate"
        save_json(artifact_dir / "manifest.json", manifest)
        return Candidate(None, artifact_dir, None)
    except BaseException as exc:
        manifest.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "blocked",
                        error_type=type(exc).__name__)
        save_json(artifact_dir / "manifest.json", manifest)
        raise
