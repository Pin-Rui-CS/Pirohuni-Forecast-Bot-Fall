"""SDK clients for a route, and the one blocking call helper.

provider.py decides where a request goes (routes, endpoints, request shaping);
this module builds the client that sends it: a cached OpenAI / AsyncOpenAI per
endpoint, or the Qwen ladder client on SoCLaaS when QWEN_LADDER=1. Kept apart
from provider.py so routing depends on nothing else in the bot.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from bot.llm import qwen_ladder
from bot.llm.cost_manager import MonetaryCostManager
from bot.llm.provider import ENDPOINT_SOCLAAS, Route, api_key_for, gate_for, prepare

_async_clients: dict[tuple[str, int], Any] = {}
_sync_clients: dict[str, Any] = {}
_client_lock = threading.Lock()


def _ladder_client(route: Route, sync: bool):
    """The XHigh-then-Medium client, when QWEN_LADDER=1 and the route is SoCLaaS."""
    if route.endpoint.name != ENDPOINT_SOCLAAS.name:
        return None
    if not qwen_ladder.enabled():
        return None
    return qwen_ladder.SyncLadderClient(route) if sync else qwen_ladder.AsyncLadderClient(route)


def async_client_for(route: Route):
    """Cached AsyncOpenAI for this route's endpoint."""
    from openai import AsyncOpenAI  # local import: keeps openai an optional dep

    ladder = _ladder_client(route, sync=False)
    if ladder is not None:
        return ladder
    endpoint = route.endpoint
    # Keyed by event loop as well: an AsyncOpenAI binds its httpx pool to the
    # loop that created it. Production runs one asyncio.run(), but eval_tools
    # and any future test harness make their own.
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (endpoint.name, loop_id)
    client = _async_clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=endpoint.base_url,
            api_key=api_key_for(endpoint),
            timeout=endpoint.timeout_seconds,
            max_retries=0,
        )
        _async_clients[key] = client
    return client


def sync_client_for(route: Route):
    """Cached OpenAI client for this route's endpoint.

    Lock-guarded because the prediction-market providers call this from
    ``asyncio.to_thread`` worker threads.
    """
    from openai import OpenAI

    ladder = _ladder_client(route, sync=True)
    if ladder is not None:
        return ladder
    endpoint = route.endpoint
    with _client_lock:
        client = _sync_clients.get(endpoint.name)
        if client is None:
            client = OpenAI(
                base_url=endpoint.base_url,
                api_key=api_key_for(endpoint),
                timeout=endpoint.timeout_seconds,
                max_retries=0,
            )
            _sync_clients[endpoint.name] = client
        return client


def reset_clients() -> None:
    """Drop cached clients. For tests, and after a credential/route change."""
    _async_clients.clear()
    with _client_lock:
        _sync_clients.clear()


def sync_chat(label: str, model: str, payload: dict[str, Any]):
    """One blocking chat completion: routed, gated, and cost-accounted.

    Safe from an ``asyncio.to_thread`` worker -- the MonetaryCostManager
    ContextVar is copied into the thread, so records land in the right ledger.

    This exists because the three prediction-market providers are synchronous
    and each carried its own copy of client construction, model resolution and
    usage recording. Being sync, they also bypass the asyncio semaphore
    entirely, which is exactly why the gate has to be acquired here too.
    """
    prepared = prepare(model, payload, label=label)
    usage_handle = MonetaryCostManager.start_openrouter_call(
        label, prepared.model_id, payload
    )
    gate_for(prepared.route).wait_sync()
    response = sync_client_for(prepared.route).chat.completions.create(**prepared.kwargs)
    usage_handle.record_response(response)
    return response
