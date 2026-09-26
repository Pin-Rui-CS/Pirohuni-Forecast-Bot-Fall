from __future__ import annotations

import asyncio
import os

import dotenv

import llm_provider

dotenv.load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or default


NUM_RUNS_PER_QUESTION = 3

# --- Forecaster ensemble -----------------------------------------------------
# The per-question forecast runs are spread across an ensemble of models, mapped
# onto this pool in order and cycling when NUM_RUNS_PER_QUESTION exceeds the pool
# size. The motivation for a *multi-model* pool is that genuinely different model
# lineages make different errors, so aggregating across them decorrelates those
# errors -- a bigger accuracy gain than re-sampling one model at temperature. The
# pool should hold models of *comparable* forecasting quality (con 2: a
# materially weaker model drags the average instead of decorrelating it).
#
# Two-model pool since 2026-07-19 (44620 post-mortem: Opus-on-brief and
# Sonnet-on-raw made the SAME directional reasoning errors — input diversity
# cannot decorrelate shared reasoning fallacies; a different lineage might).
# With NUM_RUNS_PER_QUESTION=3 the pool maps: run 1 = Opus/brief,
# run 2 = GPT/brief (paired A/B against run 1 on the identical brief),
# run 3 = Opus -> swapped to the Sonnet raw-research member below.
# Watch the audit table's reasoning-token column on the first GPT runs:
# OpenAI bills chain-of-thought as output tokens, so the effective per-run
# cost can be a multiple of the $5/$30 sticker. Note Gemini 3.1 Pro is
# currently unreachable via the OpenRouter BYOK Google key (free tier, daily
# limit 0) until that key has billing enabled or BYOK is disabled.
#
# --- Under LLM_PROVIDER=openai ------------------------------------------------
# The pool below cannot simply be translated model-by-model: llm_provider maps
# BOTH anthropic/claude-opus-5.5 and openai/gpt-5.6-sol onto gpt-5.6-sol, which
# would make runs 1 and 2 the same model on the same brief. That is not an
# ensemble -- and because the GPT-5 models reject `temperature`, the two runs
# could not even be decorrelated by sampling. The OpenAI pool is therefore a
# capability ladder (sol + terra) rather than a lineage mix. This is the
# accepted cost of a single-vendor setup: it is weaker decorrelation than the
# cross-lineage pool above, so watch for all three runs sharing one error.
# The pool is data owned by the routing profile rather than an if/else on a
# provider boolean here: adding an endpoint should not mean editing config.py,
# and three independent is_openai() branches could drift apart. See
# llm_provider._openrouter_profile / _openai_profile, where each pool now sits
# next to the routes it names.
_profile = llm_provider.active_profile()
DEFAULT_FORECASTER_MODEL = _profile.default_forecaster
FORECASTER_MODELS = _env_list("FORECASTER_MODELS", list(_profile.forecaster_pool))
# The tiebreaker / synthesis judge is a single fixed strong model so the final
# call doesn't inherit whichever ensemble member happened to run last.
FORECASTER_TIEBREAKER_MODEL = os.getenv(
    "FORECASTER_TIEBREAKER_MODEL", DEFAULT_FORECASTER_MODEL
)

# --- Heterogeneous ensemble run ----------------------------------------------
# When enabled (and NUM_RUNS_PER_QUESTION >= 2), the LAST ensemble run reads the
# RAW research (deduplicated provider output) instead of the compiled brief, on
# HETEROGENEOUS_RUN_MODEL. Three-plus incidents (2026-06-30, 2026-07-07, 44619)
# showed N same-model runs on one compiled brief reproduce any brief-level skew
# at full weight — the ensemble measures sampling noise, not epistemic
# disagreement. A raw-input run is the only member that can catch evidence the
# compile step dropped. Cost-neutral-ish: it REPLACES a run (Sonnet's cheaper
# rate offsets the larger raw input); it does not add one.
HETEROGENEOUS_RUN_ENABLED = _env_bool("HETEROGENEOUS_RUN_ENABLED", True)
HETEROGENEOUS_RUN_MODEL = os.getenv(
    "HETEROGENEOUS_RUN_MODEL", _profile.heterogeneous_model
)
# --- Forecast output cap -----------------------------------------------------
# Ceiling on VISIBLE output tokens for a forecast run and for the binary
# tiebreaker. Until now these were the only Tier-1 calls in the repo sending no
# cap at all, which was harmless on Opus-5/5.5 ($25 and $20/1M out) and is not on a
# reasoning model priced at $50/1M with a 128K completion limit: one run could
# bill more than a whole question is budgeted for.
#
# Sized from measured output on the two audited runs -- 9,800 / 11,124 / 12,307
# / 12,587 / 15,437 / 16,569 tokens -- so 20K is roughly 1.2x the worst
# observed, not a target. It is a ceiling; routes that need room for internal
# reasoning on top of the visible answer get it added by the route's own
# reasoning_headroom_tokens in build_kwargs.
#
# If a run hits this, call_llm now logs a TRUNCATED warning naming the cap, and
# the run will usually fail its validator (these prompts put the answer last)
# and take its one repair retry. Raise the cap rather than letting that recur.
FORECAST_MAX_OUTPUT_TOKENS = int(os.getenv("FORECAST_MAX_OUTPUT_TOKENS", "20000"))

SKIP_PREVIOUSLY_FORECASTED_QUESTIONS = True
METACULUS_MAX_CONCURRENT_REQUESTS = int(os.getenv("METACULUS_MAX_CONCURRENT_REQUESTS", "1"))
METACULUS_API_RATE_LIMITER = asyncio.Semaphore(METACULUS_MAX_CONCURRENT_REQUESTS)
METACULUS_REQUEST_INTERVAL = float(os.getenv("METACULUS_REQUEST_INTERVAL", "3.0"))

METACULUS_TOKEN = os.getenv("METACULUS_TOKEN")
ASKNEWS_CLIENT_ID = os.getenv("ASKNEWS_CLIENT_ID")
ASKNEWS_SECRET = os.getenv("ASKNEWS_SECRET")
ASKNEWS_API_KEY = os.getenv("ASKNEWS_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
OPENROUTER_COST_HARD_LIMIT_USD = float(os.getenv("OPENROUTER_COST_HARD_LIMIT_USD", "0"))

# Central research provider toggles.
ENABLE_ASKNEWS_RESEARCH = _env_bool("ENABLE_ASKNEWS_RESEARCH", True)
ENABLE_RESOLUTION_SOURCE_RESEARCH = _env_bool("ENABLE_RESOLUTION_SOURCE_RESEARCH", True)
# Search providers run as a priority fallback chain (SerpAPI -> Tavily ->
# Firecrawl): the bot uses the first enabled provider that returns results and
# skips the rest, so enabling all three conserves credits rather than spending
# them in parallel.
ENABLE_SERPAPI_RESEARCH = _env_bool("ENABLE_SERPAPI_RESEARCH", True)
ENABLE_FIRECRAWL_RESEARCH = _env_bool("ENABLE_FIRECRAWL_RESEARCH", True)
ENABLE_TAVILY_RESEARCH = _env_bool("ENABLE_TAVILY_RESEARCH", True)
ENABLE_PREDICTION_MARKET_RESEARCH = _env_bool("ENABLE_PREDICTION_MARKET_RESEARCH", True)
# API agent (research/apiagent_research.py): a Qwen tool loop over apiagent-kit's
# public data APIs. Runs only on SoCLaaS Qwen, never a paid model; skipped when
# SoCLaaS or Node is unavailable.
ENABLE_APIAGENT_RESEARCH = _env_bool("ENABLE_APIAGENT_RESEARCH", True)
APIAGENT_TIMEOUT_SECONDS = float(os.getenv("APIAGENT_TIMEOUT_SECONDS") or "900")
APIAGENT_MAX_STEPS = int(os.getenv("APIAGENT_MAX_STEPS") or "6")
FIRECRAWL_SEARCH_TBS = os.getenv("FIRECRAWL_SEARCH_TBS", "").strip()
# Firecrawl-first scraping of general research URLs (the serp_research scrape
# cycles). Resolution-criteria scraping always tries Firecrawl when a key is
# present; this toggle only governs the general research path.
ENABLE_FIRECRAWL_GENERAL_SCRAPE = _env_bool("ENABLE_FIRECRAWL_GENERAL_SCRAPE", True)
# Hard per-question Firecrawl credit cap shared by the resolution and general
# research paths. Resolution URLs get first claim: until the resolution scraper
# finishes, general research must leave FIRECRAWL_RESOLUTION_CREDIT_RESERVE
# credits of the cap untouched (10 matches scrape_resolution_sources'
# max_urls). See research/firecrawl_scrape.py for the budget semantics.
FIRECRAWL_QUESTION_CREDIT_CAP = int(os.getenv("FIRECRAWL_QUESTION_CREDIT_CAP", "25"))
FIRECRAWL_RESOLUTION_CREDIT_RESERVE = int(
    os.getenv("FIRECRAWL_RESOLUTION_CREDIT_RESERVE", "10")
)
# Tavily search depth: basic|advanced|fast|ultra-fast (basic = 1 credit/query).
TAVILY_SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "basic").strip()
# Research evolution trace: per-question trace/ folder + evolution.md showing
# how the research changed step by step (see docs/research_trace_plan.md).
# Deterministic write-through only — zero added LLM calls, never fails a run.
ENABLE_RESEARCH_TRACE = _env_bool("ENABLE_RESEARCH_TRACE", True)

# Tournament IDs
Q4_2024_AI_BENCHMARKING_ID = 32506
Q1_2025_AI_BENCHMARKING_ID = 32627
FALL_2025_AI_BENCHMARKING_ID = "fall-aib-2025"
SPRING_2026_AI_BENCHMARKING_ID = "spring-aib-2026"
SUMMER_2026_AI_BENCHMARKING_ID = "summer-futureeval-2026"
# Current season: Fall FutureEval 2026, Metaculus project 33121. The slug and
# the numeric id are interchangeable in the /posts/ `tournaments=` query; the
# slug is used here to match every other post-2025 tournament.
FALL_2026_AI_BENCHMARKING_ID = "fall-futureeval-2026"
CURRENT_MINIBENCH_ID = "minibench"

Q4_2024_QUARTERLY_CUP_ID = 3672
Q1_2025_QUARTERLY_CUP_ID = 32630
SUMMER_2026_METACULUS_CUP_ID = "metaculus-cup-summer-2026"
# Both metaculus-cup-* workflows resolve their question list through this
# constant, so bumping it each season is all they need.
CURRENT_METACULUS_CUP_ID = "metaculus-cup-fall-2026"

AXC_2025_TOURNAMENT_ID = 32564
AI_2027_TOURNAMENT_ID = "ai-2027"

# Used when --tournament is omitted. Points at the seasonal AI-benchmarking
# tournament rather than the cup, because that is what the scheduled workflows
# actually forecast on; the cup is reached explicitly, via --tournament
# metaculus-cup or the two metaculus-cup-* workflows.
DEFAULT_TOURNAMENT_ID = FALL_2026_AI_BENCHMARKING_ID

TOURNAMENT_MAPPING = {
    "q4-2024-ai": Q4_2024_AI_BENCHMARKING_ID,
    "q1-2025-ai": Q1_2025_AI_BENCHMARKING_ID,
    "fall-2025-ai": FALL_2025_AI_BENCHMARKING_ID,
    "spring-2026-ai": SPRING_2026_AI_BENCHMARKING_ID,
    "summer-2026-ai": SUMMER_2026_AI_BENCHMARKING_ID,
    "fall-2026-ai": FALL_2026_AI_BENCHMARKING_ID,
    "minibench": CURRENT_MINIBENCH_ID,
    "q4-2024-cup": Q4_2024_QUARTERLY_CUP_ID,
    "q1-2025-cup": Q1_2025_QUARTERLY_CUP_ID,
    "summer-2026-cup": SUMMER_2026_METACULUS_CUP_ID,
    "metaculus-cup": CURRENT_METACULUS_CUP_ID,
    "axc-2025": AXC_2025_TOURNAMENT_ID,
    "ai-2027": AI_2027_TOURNAMENT_ID,
}

EXAMPLE_QUESTIONS: list[tuple[int, int]] = [
    (578, 578),      # Human Extinction - Binary
    (14333, 14333),  # Age of Oldest Human - Numeric
    (22427, 22427),  # Number of New Leading AI Labs - Multiple Choice
    (38195, 38880),  # Number of US Labor Strikes Due to AI in 2029 - Discrete
]

AUTH_HEADERS = {"Authorization": f"Token {METACULUS_TOKEN}"}
API_BASE_URL = "https://www.metaculus.com/api"
MAX_API_GET_RETRIES = 3
INITIAL_API_GET_RETRY_WAIT_SECONDS = float(os.getenv("INITIAL_API_GET_RETRY_WAIT_SECONDS", "3.0"))

CONCURRENT_REQUESTS_LIMIT = int(os.getenv("LLM_CONCURRENT_REQUESTS", "5"))
llm_rate_limiter = asyncio.Semaphore(CONCURRENT_REQUESTS_LIMIT)
