import type { ApiAdapter } from "./types.ts";
import { arxiv } from "./adapters/arxiv.ts";
import { cboeVix } from "./adapters/cboe-vix.ts";
import { cisaKev } from "./adapters/cisa-kev.ts";
import { clinicalTrials } from "./adapters/clinicaltrials.ts";
import { cpcEnso } from "./adapters/cpc-enso.ts";
import { coinbase } from "./adapters/coinbase.ts";
import { fdic } from "./adapters/fdic.ts";
import { federalRegister } from "./adapters/federal-register.ts";
import { fred } from "./adapters/fred.ts";
import { github } from "./adapters/github.ts";
import { gdelt } from "./adapters/gdelt.ts";
import { iemAsos } from "./adapters/iem-asos.ts";
import { launchLibrary } from "./adapters/launch-library.ts";
import { kalshi } from "./adapters/kalshi.ts";
import { metaculus } from "./adapters/metaculus.ts";
import { nhc } from "./adapters/nhc.ts";
import { nhcHistory } from "./adapters/nhc-history.ts";
import { nhcOutlook } from "./adapters/nhc-outlook.ts";
import { openFda } from "./adapters/openfda.ts";
import { pageviews } from "./adapters/pageviews.ts";
import { polymarket } from "./adapters/polymarket.ts";
import { portwatch } from "./adapters/portwatch.ts";
import { secEdgar } from "./adapters/sec-edgar.ts";
import { treasuryFiscal } from "./adapters/treasury-fiscal.ts";
import { usgsEarthquakes } from "./adapters/usgs-earthquakes.ts";
import { who } from "./adapters/who.ts";
import { wikipedia } from "./adapters/wikipedia.ts";
import { worldBank } from "./adapters/worldbank.ts";

/**
 * The single place that knows every API exists.
 *
 * Same discipline as `projects/registry.ts` one level up, and for the same
 * reason: a hand-written array trades a remembered line for build magic, which
 * is a good trade at this size. Adding an API is one adapter file and one entry
 * here — no prompt change, no tool-schema change, because the model never sees
 * this list. It sees only what `find` returns.
 *
 * Twenty-eight adapters. Twenty-six are keyless; FRED and Metaculus self-disable
 * without a key and are simply absent until one is set. The three tropical and
 * climate adapters (nhc_history, nhc_outlook, cpc_enso) were added after Q46147,
 * whose decisive evidence (HURDAT2 base rates split by ENSO, the NHC outlook,
 * CPC's ENSO and weeks 2-3 discussions) search could not reach. They and
 * nhc_storms set `keywordsRequired` with domain-only keywords (no "season",
 * "outlook", "historical"): test/matcher.test.ts checks every real need the agent
 * has written so far and fails if they are offered for anything else.
 *
 * Still absent and worth adding: BLS, EIA, Census, Congress.gov, CourtListener,
 * LegiScan, Regulations.gov. Each needs a free key, which is the only reason
 * they are not here.
 */
export const adapters: ApiAdapter[] = [
  gdelt,
  secEdgar,
  federalRegister,
  clinicalTrials,
  openFda,
  who,
  cisaKev,
  arxiv,
  launchLibrary,
  iemAsos,
  coinbase,
  cboeVix,
  fdic,
  fred,
  treasuryFiscal,
  portwatch,
  wikipedia,
  pageviews,
  usgsEarthquakes,
  worldBank,
  nhc,
  nhcHistory,
  nhcOutlook,
  cpcEnso,
  github,
  polymarket,
  kalshi,
  metaculus,
];

/**
 * Adapters that can actually be reached right now.
 *
 * Everything below routes through this rather than `adapters`, so an API whose
 * key is missing is invisible to the model instead of being offered and then
 * failing. `adapters` stays exported for the smoke test, which wants to report
 * on the unavailable ones too.
 */
export function activeAdapters(): ApiAdapter[] {
  return adapters.filter((adapter) => adapter.available?.() !== false);
}

export function getAdapter(id: string): ApiAdapter | undefined {
  return activeAdapters().find((adapter) => adapter.id === id);
}

/**
 * Words carrying no routing signal.
 *
 * Deliberately short. An over-eager stoplist is worse than none: "will" looks
 * like noise until you notice it is what distinguishes a forecasting question
 * from a factual one, and "many" is the difference between "which banks failed"
 * and "how many banks failed". These are only the words that appear in
 * substantially every question.
 */
const STOPWORDS = new Set([
  "the", "and", "for", "was", "are", "were", "with", "that", "this", "from",
  "have", "has", "had", "what", "when", "where", "which", "who", "whom", "how",
  "does", "did", "can", "could", "would", "should", "about", "into", "than",
  "then", "there", "their", "been", "being", "any", "all", "get", "got",
  "find", "out", "some", "more", "most", "other", "such", "only", "own",
  "same", "each", "between", "during", "before", "after", "above", "below",
]);

function tokenise(text: string): string[] {
  // Accents stripped first, or "Niño" splits into "ni" + "o" and matches nothing.
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((word) => word.length >= 3 && !STOPWORDS.has(word));
}

/**
 * Crude suffix stripping, so word forms match without substrings doing it.
 *
 * This replaced a substring test, and the first attempt at replacing it — plain
 * whole-word matching — was WORSE than the bug it fixed. Both failures are
 * worth recording because they define what this function has to get right.
 *
 *  - Substring matching scored `sec_edgar` for the token "rate", because "rate"
 *    sits inside "corpo-rate-". Noise.
 *  - Whole-word matching then dropped `sec_edgar` from "Will Apple file an 8-K
 *    this quarter?" entirely: its keywords carry "filing" and "quarterly", and
 *    "file" and "quarter" are neither of those. A real match, lost.
 *
 * The distinction wanted is morphological. "file" and "filing" share a root;
 * "rate" and "corporate" do not — "rate" merely ends it. Stripping suffixes
 * from both sides captures that with no dictionary:
 *
 *    file, filing, filings -> fil        quarter, quarterly -> quarter
 *    rate -> rat           corporate -> corporat        (correctly apart)
 *
 * The length floor stops aggressive stripping collapsing short words into each
 * other, which would reintroduce noise from the other direction.
 */
function stem(word: string): string {
  let w = word;
  if (w.length > 4 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
  for (const suffix of ["ing", "ed", "ly", "e"]) {
    if (w.endsWith(suffix) && w.length - suffix.length >= 3) {
      return w.slice(0, -suffix.length);
    }
  }
  return w;
}

/**
 * Per-adapter index, built once and cached against the adapter object.
 *
 * `keywords` holds exact forms, `stems` their stripped roots, and `answers` the
 * prose tokenised the same way a query is — so a prose hit is a whole word,
 * never a fragment.
 */
type Index = { keywords: Set<string>; stems: Set<string>; answers: Set<string> };

const indexes = new WeakMap<ApiAdapter, Index>();

function indexOf(adapter: ApiAdapter): Index {
  const existing = indexes.get(adapter);
  if (existing) return existing;

  const keywords = adapter.keywords.map((keyword) => keyword.toLowerCase());
  const built: Index = {
    keywords: new Set(keywords),
    stems: new Set(keywords.map(stem)),
    answers: new Set(tokenise(adapter.answers)),
  };
  indexes.set(adapter, built);
  return built;
}

/**
 * Score one adapter against the tokens of a need.
 *
 * Weights follow how much each signal means. A keyword is hand-written for
 * routing, so an exact hit is worth most and a shared root nearly as much. A
 * hit in the prose of `answers` is worth least: that text is long, and even
 * matched on whole words it can coincide.
 */
function score(adapter: ApiAdapter, tokens: string[]): number {
  const index = indexOf(adapter);
  let total = 0;

  for (const token of tokens) {
    if (index.keywords.has(token)) total += 3;
    else if (index.stems.has(stem(token))) total += 2;

    if (adapter.domain === token) total += 2;
    if (index.answers.has(token)) total += 1;
  }

  return total;
}

/**
 * Below this, a candidate is a coincidence rather than a match.
 *
 * Three is one exact keyword hit, or a domain hit plus a prose hit. Two lone
 * prose hits (score 2) are not enough to spend a slot on — with twenty-four
 * adapters and six slots, admitting weak matches is how a good one gets pushed
 * out of the shortlist.
 */
const MIN_SCORE = 3;

/**
 * For adapters with `keywordsRequired`: keywords route, domain and `answers`
 * prose only rank. Without it, prose words alone reached MIN_SCORE on real
 * needs: "TOP500 list ... systems ... latest" offered the hurricane outlook,
 * and "weather" (a domain) plus "forecast" in the prose offered every weather
 * adapter for a Denver game-day forecast. Other adapters are unaffected.
 */
function routed(adapter: ApiAdapter, tokens: string[]): boolean {
  if (!adapter.keywordsRequired) return true;
  const index = indexOf(adapter);
  return tokens.some((token) => index.keywords.has(token) || index.stems.has(stem(token)));
}

/**
 * Why `find` would (or would not) offer one adapter for a need: each token's
 * contribution. Used by the matcher regression test to explain a failure.
 */
export function explainScore(need: string, id: string): { total: number; hits: string[] } {
  const adapter = adapters.find((entry) => entry.id === id);
  if (!adapter) return { total: 0, hits: [] };
  const index = indexOf(adapter);
  const hits: string[] = [];
  for (const token of tokenise(need)) {
    if (index.keywords.has(token)) hits.push(`${token}:keyword+3`);
    else if (index.stems.has(stem(token))) hits.push(`${token}:stem+2`);
    if (adapter.domain === token) hits.push(`${token}:domain+2`);
    if (index.answers.has(token)) hits.push(`${token}:answers+1`);
  }
  return { total: score(adapter, tokenise(need)), hits };
}

export type Candidate = {
  id: string;
  name: string;
  tier: string;
  domain: string;
  answers: string;
  paramsHelp: string;
};

const MAX_CANDIDATES = 6;

/**
 * Candidate APIs for a stated need, best first.
 *
 * Deterministic and LLM-free, which is the point: API selection can be tested
 * without spending gateway quota, and the same question always produces the
 * same shortlist.
 *
 * GDELT is appended when nothing else matched well. Almost every forecasting
 * question has SOME news signal, so an empty shortlist is nearly always a
 * failure of the matcher rather than a real absence — and returning nothing
 * teaches the model to give up and answer from memory, which is the exact
 * behaviour this project exists to prevent.
 */
export function find(need: string): Candidate[] {
  const tokens = tokenise(need);

  const ranked = activeAdapters()
    .map((adapter) => ({ adapter, points: score(adapter, tokens) }))
    .filter((entry) => entry.points >= MIN_SCORE && routed(entry.adapter, tokens))
    .sort((a, b) => b.points - a.points)
    .slice(0, MAX_CANDIDATES)
    .map((entry) => entry.adapter);

  if (!ranked.some((adapter) => adapter.id === gdelt.id)) {
    ranked.push(gdelt);
  }

  return ranked.map((adapter) => ({
    id: adapter.id,
    name: adapter.name,
    tier: adapter.tier,
    domain: adapter.domain,
    answers: adapter.answers,
    paramsHelp: adapter.paramsHelp,
  }));
}
