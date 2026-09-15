"""Phase-1 safety net: the Route path must reproduce pre-refactor behaviour.

The route table replaced a global provider switch with per-model routing. That
is only a safe refactor if, for every request the bot actually makes, the new
path emits BYTE-IDENTICAL kwargs to what shipped before.

``tests/golden_chat_kwargs.json`` holds 550 request snapshots captured from the
ORIGINAL ``chat_kwargs`` while it was still an independent implementation
(every internal model name x every payload shape in the repo x both providers
x the env overrides that change reasoning effort and prompt caching). This
asserts ``build_kwargs(route_for(m), payload)`` still matches all of them.

The golden file is the point. Once ``chat_kwargs`` became a shim over
``build_kwargs`` a live comparison between the two would be tautological — the
snapshot is what preserves the guarantee.

    python tests/test_route_equivalence.py

Provider selection is frozen at import (``LLM_PROVIDER: Final``), so each
provider runs in its own subprocess rather than by reloading.

Regenerate the golden ONLY when a request-shaping change is intended, and say
in the commit why every changed line is correct:
    python tests/test_route_equivalence.py --regenerate
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_PATH = os.path.join(REPO_ROOT, "tests", "golden_chat_kwargs.json")

# One entry per distinct payload shape built anywhere in the repo.
PAYLOADS: list[tuple[str, dict]] = [
    ("call_llm default (no cap)",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.3, "stream": False}),
    ("call_llm utility temp 0.1",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.1, "stream": False}),
    ("call_llm query-gen temp 0.2",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.2, "stream": False}),
    ("compiler precompress (capped)",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.1, "stream": False,
      "max_tokens": 8110}),
    ("compiler brief (direct client)",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.1,
      "max_tokens": 10_000, "stream": False}),
    ("resolution wayback-history",
     {"messages": [{"role": "user", "content": "x"}], "max_tokens": 1200, "temperature": 0.1}),
    ("resolution page-summary",
     {"messages": [{"role": "user", "content": "x"}], "max_tokens": 2000, "temperature": 0.1}),
    ("market scorer temp 0",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0}),
    ("bare messages only",
     {"messages": [{"role": "user", "content": "x"}]}),
    ("caller-supplied extra_body",
     {"messages": [{"role": "user", "content": "x"}], "temperature": 0.1,
      "extra_body": {"stale": True}}),
    ("cap above the ceiling",
     {"messages": [{"role": "user", "content": "x"}], "max_tokens": 200_000}),
]

# Env permutations that change the OpenAI branch's output. Running them against
# openrouter too proves they are correctly ignored there.
ENV_CASES: list[tuple[str, dict[str, str]]] = [
    ("defaults", {}),
    ("global effort override", {"OPENAI_REASONING_EFFORT": "xhigh"}),
    ("per-model effort override", {"OPENAI_REASONING_EFFORT_GPT_5_6_SOL": "minimal"}),
    ("implicit prompt cache", {"OPENAI_PROMPT_CACHE": "implicit"}),
    ("effort none", {"OPENAI_REASONING_EFFORT": "none"}),
]

_CHILD = r"""
import json, sys
sys.path.insert(0, %(root)r)
import llm_provider as lp
payloads = json.loads(sys.argv[1])
out = {}
for name in lp.INTERNAL_MODEL_NAMES:
    for label, payload in payloads:
        out[name + "||" + label] = lp.build_kwargs(lp.route_for(name), dict(payload))
print(json.dumps(out, sort_keys=True))
"""


def _emit(provider: str, extra_env: dict[str, str]) -> dict:
    """Run build_kwargs over every model x payload in a clean subprocess."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OPENAI_REASONING_EFFORT")
           and k not in {"OPENAI_PROMPT_CACHE", "LLM_ROUTING"}}
    # Deliberately set LLM_ROUTING to the EMPTY STRING rather than dropping it.
    # The child imports llm_provider, which calls dotenv.load_dotenv() -- so a
    # repo-root .env carrying `LLM_ROUTING=openrouter` would be read into the
    # child and, because LLM_ROUTING outranks LLM_PROVIDER, would silently pin
    # every case to openrouter (275 spurious mismatches, all on the openai
    # side). load_dotenv does not overwrite a key already present in the
    # environment, even an empty one, so this blocks the file; and
    # _read_routing_mode's `or` treats "" as unset, so the LLM_PROVIDER alias
    # below is still the thing under test.
    env["LLM_ROUTING"] = ""
    env["LLM_PROVIDER"] = provider
    env.update(extra_env)
    child = _CHILD % {"root": REPO_ROOT}
    proc = subprocess.run(
        [sys.executable, "-c", child, json.dumps(PAYLOADS)],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"child crashed for {provider}/{extra_env}:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _collect() -> dict[str, dict]:
    return {
        f"{provider}||{env_label}": _emit(provider, extra_env)
        for provider in ("openrouter", "openai")
        for env_label, extra_env in ENV_CASES
    }


def main() -> int:
    if "--regenerate" in sys.argv:
        with open(GOLDEN_PATH, "w", encoding="utf-8") as handle:
            json.dump(_collect(), handle, indent=1, sort_keys=True)
        print(f"regenerated {GOLDEN_PATH}")
        return 0

    with open(GOLDEN_PATH, encoding="utf-8") as handle:
        golden = json.load(handle)

    actual = _collect()
    checked = bad = 0
    for case, expected_requests in sorted(golden.items()):
        got = actual.get(case, {})
        mismatches = []
        for key, expected in expected_requests.items():
            checked += 1
            if got.get(key) != expected:
                mismatches.append((key, expected, got.get(key)))
        print(f"  {'ok  ' if not mismatches else 'FAIL'} {case:<40} "
              f"{len(expected_requests):>3} requests")
        for key, expected, found in mismatches:
            bad += 1
            model, _, payload = key.partition("||")
            print(f"        model={model}  payload={payload}")
            print(f"          golden      : {expected}")
            print(f"          build_kwargs: {found}")

    missing = set(golden) - set(actual)
    extra = set(actual) - set(golden)
    for case in sorted(missing):
        print(f"  FAIL missing case in output: {case}")
        bad += 1
    for case in sorted(extra):
        print(f"  FAIL case not in golden: {case}")
        bad += 1

    print(f"\n{checked} requests compared, {bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
