import type { z } from "zod";

/**
 * The adapter contract.
 *
 * Shaped after `lib/search.ts`'s `SearchProvider`, which is this repo's
 * established answer to "many backends, one interface": an interface, a closure
 * per backend, a list of the configured ones. The difference is that search has
 * two providers and this has forty-three, so the model cannot be shown all of
 * them at once — hence the registry + dispatcher in `registry.ts` and
 * `tools.ts`.
 */

/**
 * Provenance, from `api-registry-crossref.md` §3.
 *
 * Set by the adapter, never by the model, so it cannot be talked out of. The
 * distinction the whole document turns on is that "useful for forecasting" and
 * "valid for resolution" are not the same claim.
 *
 *  A — resolution-grade. Documented, stable, citable as the answer.
 *  B — forecasting input. Documented, but not the named resolution source.
 *  C — fragile input. Undocumented internal endpoint. Must be logged with its
 *      retrieval URL and timestamp, must never be cited as resolution, and must
 *      fail loudly rather than silently when its shape changes.
 */
export type Tier = "A" | "B" | "C";

/** Coarse subject area. Used by the matcher and shown in the picker. */
export type Domain =
  | "events"
  | "filings"
  | "regulation"
  | "health"
  | "security"
  | "science"
  | "weather"
  | "markets"
  | "banking"
  | "reference"
  | "forecasting"
  | "geophysical"
  | "statistics"
  | "technology";

/**
 * What an adapter hands back.
 *
 * `url` and `retrievedAt` are required rather than optional because §3's Tier C
 * policy demands both, and an audit trail that exists only for the tier that
 * remembered to fill it in is not an audit trail.
 */
export type AdapterResult = {
  tier: Tier;
  /** The exact URL retrieved. Rendered in the UI as the provenance link. */
  url: string;
  /** ISO instant of retrieval. */
  retrievedAt: string;
  /** Normalised, capped. Shape is per-adapter and documented in `paramsHelp`. */
  rows: unknown[];
  /** True when `MAX_ROWS` cut the list — the answer is partial, not complete. */
  truncated?: boolean;
  /**
   * Caveat the model must carry into its answer. Publication lag, seasonal
   * adjustment, "this is the venue print not the index" — the distinctions §6
   * flags as a live risk across roughly fifteen of the market questions.
   */
  note?: string;
};

/** The per-adapter half, written by each adapter file. */
export type AdapterSpec<P> = {
  /** Stable machine name. Travels in tool calls and persisted conversations. */
  id: string;
  /** Shown in the UI and in `find_apis` output. */
  name: string;
  tier: Tier;
  domain: Domain;
  /**
   * What questions this can answer, in prose. Goes to the model verbatim, so it
   * is the entire basis on which an API gets chosen — write criteria, not
   * capability, and say what it is NOT for.
   */
  answers: string;
  /** Matcher fuel. Never shown to the model. */
  keywords: string[];
  paramsSchema: z.ZodType<P>;
  /** How to fill the params, in prose. Also goes to the model verbatim. */
  paramsHelp: string;
  /**
   * Whether this API can be reached at all right now — almost always "is its
   * key configured".
   *
   * Same reasoning as `listSearchProviders()` in `lib/search.ts`: an API with
   * no credential is not a broken API, it is an absent one, and offering the
   * model something that cannot work is worse than not offering it. Omit for
   * the keyless majority.
   */
  available?: () => boolean;
  run(params: P, signal: AbortSignal): Promise<AdapterResult>;
};

/**
 * The type-erased form the registry stores.
 *
 * Adapters have different param types, so they cannot sit in one array without
 * erasing that type somewhere. Doing it here — behind `defineAdapter`, which
 * re-parses on the way in — keeps every adapter body fully typed and confines
 * the untyped boundary to a single function.
 */
export type ApiAdapter = Omit<AdapterSpec<unknown>, "paramsSchema" | "run"> & {
  paramsSchema: z.ZodType<unknown>;
  run(params: unknown, signal: AbortSignal): Promise<AdapterResult>;
};

/**
 * Erase the params type, validating on the way through.
 *
 * The tool layer parses too, so it can return a helpful error to the model
 * rather than throwing. This second parse is what makes the erasure sound: a
 * caller reaching `run` with the wrong shape gets a zod error, not an
 * unchecked cast.
 */
export function defineAdapter<P>(spec: AdapterSpec<P>): ApiAdapter {
  return {
    id: spec.id,
    name: spec.name,
    tier: spec.tier,
    domain: spec.domain,
    answers: spec.answers,
    keywords: spec.keywords,
    paramsHelp: spec.paramsHelp,
    available: spec.available,
    paramsSchema: spec.paramsSchema as z.ZodType<unknown>,
    run: (params, signal) => spec.run(spec.paramsSchema.parse(params), signal),
  };
}
