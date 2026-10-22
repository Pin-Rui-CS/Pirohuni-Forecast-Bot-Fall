"""Probe a SoCLaaS model before adding it to bot/llm/soclaas_models.py.

    python -m bot.llm.soclaas_probe x-test-2

Reads the model's quota rates from GET /v1/models, then sends one tiny chat
call per reasoning effort (and one with top_k) and reports which the gateway
accepts and the model name it answers with. SoCLaaS only, so it costs no
money; each call spends a few hundred tokens of quota. Prints a draft
SoclaasModel entry to paste into SOCLAAS_MODELS.
"""
from __future__ import annotations

import argparse
import sys

import httpx

from bot.llm import provider as llm_provider

EFFORTS = ("max", "xhigh", "high", "medium", "low", "none")
# Deepest first, matching SoclaasModel.efforts. "max" is left out of the
# drafted ladder: it is the gateway's longest setting and untested here.
LADDER_ORDER = ("xhigh", "high", "medium", "low")
MESSAGES = [{"role": "user", "content": "Reply with the single word OK."}]


def _post(client: httpx.Client, url: str, payload: dict) -> tuple[int, str, str]:
    """(HTTP status, replied model name, error text)."""
    try:
        response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        return 0, "", f"{type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return response.status_code, "", response.text[:300]
    try:
        body = response.json()
    except ValueError:
        return response.status_code, "", "reply was not JSON"
    return response.status_code, str(body.get("model") or ""), ""


def probe(model_id: str) -> int:
    endpoint = llm_provider.ENDPOINT_SOCLAAS
    key = llm_provider.api_key_for(endpoint)
    if not key:
        print("SOCLAAS_API_KEY is not set", file=sys.stderr)
        return 2
    base = endpoint.base_url.rstrip("/")
    with httpx.Client(headers={"Authorization": "Bearer " + key}, timeout=180) as client:
        listing = client.get(base + "/models")
        listing.raise_for_status()
        entry = next((m for m in listing.json().get("data", []) if m.get("id") == model_id), None)
        if entry is None:
            print(f"{model_id!r} is not in GET /v1/models", file=sys.stderr)
            return 1
        meta = entry.get("soclaas") or {}
        rates = (float(meta.get("input_microdollars_per_million_tokens") or 0),
                 float(meta.get("output_microdollars_per_million_tokens") or 0))
        print(f"{model_id}: {meta.get('display_name', '')}")
        print(f"  context: {entry.get('context_length')}  quota rate (microdollars/1M in, out): {rates}")

        url = base + "/chat/completions"
        accepted: list[str] = []
        reply_names: set[str] = set()
        for effort in EFFORTS:
            payload = {"model": model_id, "messages": MESSAGES, "max_tokens": 512,
                       "reasoning_effort": effort, "temperature": 1, "top_p": .95}
            status, replied, error = _post(client, url, payload)
            if status == 200:
                accepted.append(effort)
                if replied:
                    reply_names.add(replied)
                print(f"  effort {effort:<6} accepted, replied as {replied or '?'}")
            else:
                print(f"  effort {effort:<6} HTTP {status}: {error}")

        top_k_ok = False
        if accepted:
            payload = {"model": model_id, "messages": MESSAGES, "max_tokens": 512,
                       "reasoning_effort": accepted[-1], "temperature": 1, "top_p": .95, "top_k": 20}
            status, _, error = _post(client, url, payload)
            top_k_ok = status == 200
            print(f"  top_k=20 {'accepted' if top_k_ok else f'HTTP {status}: {error}'}")

    ladder = tuple(e for e in LADDER_ORDER if e in accepted)
    if not ladder:
        print("\nNo usable reasoning effort; do not add this model.")
        return 1
    # A two-rung ladder (deepest, lightest) like the registered models.
    ladder = ladder if len(ladder) <= 2 else (ladder[0], ladder[-1])
    no_think = "none" if "none" in accepted else ladder[-1]
    sampler = {"temperature": 1, "top_p": .95, **({"top_k": 20} if top_k_ok else {})}
    others = tuple(sorted(reply_names - {model_id}))
    print("\nDraft entry for SOCLAAS_MODELS (check the efforts suit long research prompts):")
    print(f"""    SoclaasModel(
        {model_id!r},
        reply_names={others!r},
        efforts={ladder!r},
        no_think_effort={no_think!r},
        sampler={sampler!r},
        quota_rate=({rates[0]:_.1f}, {rates[1]:_.1f}),
        note={meta.get('display_name', '')!r},
    ),""")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model_id", help="SoCLaaS model id, e.g. x-test-2")
    return probe(parser.parse_args(argv).model_id)


if __name__ == "__main__":
    raise SystemExit(main())
