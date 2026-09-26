import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * FRED — the St. Louis Fed's economic time series.
 *
 * This adapter exists as much for its `info` mode as for its data, because two
 * rules in the agent's system prompt were unenforceable without it:
 *
 *   "Check whether the figure exists yet. A period that has ended is not the
 *    same as a number that has been released."
 *   "If a source exposes a vintage or a last-updated field, name it."
 *
 * `api-registry-crossref.md` §5 names both mechanisms and neither was wired:
 * `fred/release/dates` is "the temporal-feasibility gate", currently "being
 * inferred from prose"; `fred/series` metadata carries `last_updated` and
 * `realtime_start`, which "let you detect a revision rather than silently
 * forecasting against a revised vintage" — a problem §5 says the companion
 * guide raises without naming the fix.
 *
 * `info` answers both in one call by chaining three endpoints server-side
 * (series -> its release -> that release's dates), so the model never has to
 * know that "when is the next CPI print" is three hops.
 *
 * TIER. Marked A, with a standing caveat rather than a blanket claim. FRED
 * REDISTRIBUTES: CPI and unemployment are BLS figures, housing starts are
 * Census, petroleum stocks are EIA. For most forecasting questions the FRED
 * series is accepted as the number. For a question that names the issuing
 * agency's own release, FRED is a mirror and the agency is the resolution
 * source. Every result says so.
 *
 * VERIFIED LIVE 2026-09-08, all three modes. `info` on CPIAUCSL correctly
 * reported July as the newest published month with the next release due on
 * 11 September — the temporal-feasibility gate working end to end.
 */

const params = z.object({
  mode: z
    .enum(["observations", "info", "search"])
    .describe(
      "observations = the data points. info = release schedule and vintage, " +
        "use it to check whether a figure has been published yet. " +
        "search = find a series id from a description.",
    ),
  seriesId: z
    .string()
    .regex(/^[A-Za-z0-9_.-]{2,64}$/)
    .optional()
    .describe(
      'FRED series id. Required for observations and info. Common ones: ' +
        'CPIAUCSL (CPI, seasonally adjusted), UNRATE (unemployment rate), ' +
        'GDPC1 (real GDP), DGS10 (10-year Treasury), FEDFUNDS, ' +
        'CSUSHPISA (Case-Shiller national, seasonally adjusted).',
    ),
  query: z
    .string()
    .optional()
    .describe('For mode=search, e.g. "median household income".'),
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Earliest observation date, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Latest observation date, YYYY-MM-DD."),
  // Not from upstream: lets a program (the bot's measured-series path) take a
  // whole series. Agent callers omit it and keep the 25-row default.
  limit: z.number().int().min(1).max(100_000).optional(),
});

type Params = z.infer<typeof params>;

type Series = {
  id?: string;
  title?: string;
  units?: string;
  frequency?: string;
  seasonal_adjustment?: string;
  observation_start?: string;
  observation_end?: string;
  last_updated?: string;
  notes?: string;
};

type SeriesResponse = { seriess?: Series[] };
type ObservationsResponse = {
  observations?: Array<{ date?: string; value?: string; realtime_start?: string }>;
};
type ReleaseResponse = { releases?: Array<{ id?: number; name?: string }> };
type ReleaseDatesResponse = {
  release_dates?: Array<{ release_id?: number; date?: string }>;
};

const BASE = "https://api.stlouisfed.org/fred";

/**
 * FRED gets its own, longer ceiling.
 *
 * MEASURED 2026-09-08 from this network: identical requests to
 * `api.stlouisfed.org` returned in 1.4s, 2.0s, 5.1s, 11.1s, 12.9s and 13.1s on
 * consecutive attempts, while curl fetched the same URL in 0.67s. Not a cold
 * start (it recurs warm), not a User-Agent problem (it recurs across four
 * different ones) — the host is simply erratic through its CDN.
 *
 * Under the old 10s default this made FRED fail intermittently and invisibly:
 * the agent reported "that API did not respond in time" and gave up on a
 * question it could have answered.
 */
const TIMEOUT_MS = 30_000;

function apiKey(): string {
  const key = process.env.FRED_API_KEY?.trim();
  if (!key) throw new Error("FRED_API_KEY is not configured.");
  return key;
}

function endpoint(path: string, query: Record<string, string>): URL {
  const url = new URL(`${BASE}/${path}`);
  for (const [name, value] of Object.entries(query)) {
    url.searchParams.set(name, value);
  }
  url.searchParams.set("api_key", apiKey());
  url.searchParams.set("file_type", "json");
  return url;
}

/** The key must never travel into a result — the URL is shown in the UI and
 *  persisted as the provenance log. */
function redact(url: URL): string {
  const copy = new URL(url.toString());
  copy.searchParams.set("api_key", "REDACTED");
  return copy.toString();
}

const MIRRORS =
  "FRED redistributes data it does not produce: CPI and unemployment are BLS, " +
  "housing starts are Census, petroleum stocks are EIA. If the question names " +
  "the issuing agency's own release, FRED is a mirror of it and the agency is " +
  "the resolution source.";

async function runSearch(input: Params, signal: AbortSignal) {
  if (!input.query) throw new Error("query is required when mode is search.");

  const url = endpoint("series/search", {
    search_text: input.query,
    limit: "15",
    order_by: "popularity",
    sort_order: "desc",
  });

  const body = await getJson<SeriesResponse>(url.toString(), {
    signal,
    timeoutMs: TIMEOUT_MS,
    context: "FRED search",
    expect: (value) => isObject(value) && Array.isArray(value.seriess),
    expected: "an object with a seriess array",
  });

  const { rows, truncated } = capRows(
    (body.seriess ?? []).map((series) => ({
      seriesId: series.id,
      title: truncate(series.title ?? "", 200),
      units: series.units,
      frequency: series.frequency,
      seasonalAdjustment: series.seasonal_adjustment,
      covers: `${series.observation_start} to ${series.observation_end}`,
    })),
  );

  return {
    tier: "A" as const,
    url: redact(url),
    retrievedAt: now(),
    rows,
    truncated,
    note:
      "Series ids only — call again with mode=observations to get the data. " +
      "Check seasonalAdjustment before using one: a question that says " +
      "'seasonally adjusted' needs the SA series, and the two differ.",
  };
}

/**
 * Metadata plus the release calendar: has this figure landed, and is what I am
 * looking at the current vintage?
 */
async function runInfo(input: Params, signal: AbortSignal) {
  if (!input.seriesId) throw new Error("seriesId is required when mode is info.");

  const seriesUrl = endpoint("series", { series_id: input.seriesId });
  const seriesBody = await getJson<SeriesResponse>(seriesUrl.toString(), {
    signal,
    timeoutMs: TIMEOUT_MS,
    context: "FRED series",
    expect: (value) => isObject(value) && Array.isArray(value.seriess),
    expected: "an object with a seriess array",
  });

  const series = seriesBody.seriess?.[0];
  if (!series) {
    return {
      tier: "A" as const,
      url: redact(seriesUrl),
      retrievedAt: now(),
      rows: [],
      note: `No FRED series with id "${input.seriesId}". Try mode=search.`,
    };
  }

  /*
   * The release calendar, two hops behind the series.
   *
   * Decorative relative to the metadata, so a failure here must not lose the
   * `last_updated` and `observation_end` we already have — those alone answer
   * the vintage half of the question.
   */
  let releaseName: string | undefined;
  let upcoming: string[] = [];
  let recent: string[] = [];

  try {
    const releaseUrl = endpoint("series/release", { series_id: input.seriesId });
    const releaseBody = await getJson<ReleaseResponse>(releaseUrl.toString(), {
      signal,
      timeoutMs: TIMEOUT_MS,
      context: "FRED release",
      expect: (value) => isObject(value) && Array.isArray(value.releases),
      expected: "an object with a releases array",
    });

    const release = releaseBody.releases?.[0];
    releaseName = release?.name;

    if (release?.id !== undefined) {
      const today = new Date().toISOString().slice(0, 10);
      const datesUrl = endpoint("release/dates", {
        release_id: String(release.id),
        include_release_dates_with_no_data: "true",
        sort_order: "asc",
        realtime_start: today,
        limit: "5",
      });
      const pastUrl = endpoint("release/dates", {
        release_id: String(release.id),
        sort_order: "desc",
        limit: "3",
      });

      /*
       * The two calendar lookups are independent, so they go together.
       *
       * This matters more here than the code suggests. `info` was four
       * SEQUENTIAL requests to a host measured at up to 13s each, which is how
       * a mode that answers in under two seconds on a good day could take the
       * better part of a minute on a bad one. Running these two concurrently
       * makes it three round trips instead of four.
       */
      const [datesBody, pastBody] = await Promise.all([
        getJson<ReleaseDatesResponse>(datesUrl.toString(), {
          signal,
          timeoutMs: TIMEOUT_MS,
          context: "FRED release dates",
          expect: (value) => isObject(value) && Array.isArray(value.release_dates),
          expected: "an object with a release_dates array",
        }),
        getJson<ReleaseDatesResponse>(pastUrl.toString(), {
          signal,
          timeoutMs: TIMEOUT_MS,
          context: "FRED release dates",
          expect: (value) => isObject(value) && Array.isArray(value.release_dates),
          expected: "an object with a release_dates array",
        }),
      ]);

      upcoming = (datesBody.release_dates ?? [])
        .map((entry) => entry.date)
        .filter((date): date is string => Boolean(date) && date! >= today)
        .slice(0, 3);
      recent = (pastBody.release_dates ?? [])
        .map((entry) => entry.date)
        .filter((date): date is string => Boolean(date));
    }
  } catch {
    // Leave the calendar empty and say so in the note below.
  }

  return {
    tier: "A" as const,
    url: redact(seriesUrl),
    retrievedAt: now(),
    rows: [
      {
        seriesId: series.id,
        title: series.title,
        units: series.units,
        frequency: series.frequency,
        seasonalAdjustment: series.seasonal_adjustment,
        observationStart: series.observation_start,
        // The two fields that answer "has it landed" and "which vintage".
        latestObservation: series.observation_end,
        lastUpdated: series.last_updated,
        release: releaseName,
        recentReleaseDates: recent,
        nextReleaseDates: upcoming,
      },
    ],
    note:
      `latestObservation is the most recent period WITH DATA — anything after ` +
      `it has not been published, whatever the calendar says. lastUpdated is ` +
      `when FRED last changed the series, so a date after the print means it ` +
      `has been revised. ` +
      (upcoming.length
        ? `Next scheduled release: ${upcoming.join(", ")}. `
        : "Release calendar unavailable; rely on latestObservation. ") +
      MIRRORS,
  };
}

async function runObservations(input: Params, signal: AbortSignal) {
  if (!input.seriesId) {
    throw new Error("seriesId is required when mode is observations.");
  }

  const query: Record<string, string> = {
    series_id: input.seriesId,
    sort_order: "desc",
    limit: String(input.limit ?? 60),
  };
  if (input.from) query.observation_start = input.from;
  if (input.to) query.observation_end = input.to;

  const url = endpoint("series/observations", query);
  const body = await getJson<ObservationsResponse>(url.toString(), {
    signal,
    timeoutMs: TIMEOUT_MS,
    context: "FRED observations",
    expect: (value) => isObject(value) && Array.isArray(value.observations),
    expected: "an object with an observations array",
  });

  /*
   * FRED writes a missing value as ".", not null or zero.
   *
   * Coercing that with Number() yields NaN, and anything that then treats it as
   * a number produces a silently wrong average. Dropped explicitly, and the
   * count of dropped points is reported so an unexpectedly sparse series is
   * visible rather than invisible.
   */
  const all = body.observations ?? [];
  const points = all.flatMap((observation) =>
    observation.value && observation.value !== "."
      ? [{ date: observation.date, value: Number(observation.value) }]
      : [],
  );
  const missing = all.length - points.length;

  const { rows, truncated } = capRows(points, input.limit);

  return {
    tier: "A" as const,
    url: redact(url),
    retrievedAt: now(),
    rows,
    truncated,
    note:
      `${points.length} observations, newest first` +
      (missing > 0 ? `; ${missing} periods had no value and were dropped` : "") +
      ". This is the CURRENT vintage: revised figures replace the originals in " +
      "place, so a historical value here may not be what was printed at the " +
      "time. Use mode=info to see lastUpdated and the release schedule. " +
      MIRRORS,
  };
}

export const fred = defineAdapter({
  id: "fred",
  name: "FRED (St. Louis Fed)",
  tier: "A",
  domain: "markets",
  answers:
    "US and international economic time series: inflation and CPI, " +
    "unemployment, GDP, interest and Treasury rates, money supply, house " +
    "prices, industrial production, exchange rates, oil and energy prices — " +
    "hundreds of thousands of series. Use mode=search to find a series id, " +
    "mode=observations for the numbers, and mode=info to check WHETHER A " +
    "FIGURE HAS BEEN PUBLISHED YET and when the next release is due. Reach for " +
    "info before concluding that a recent period is missing. Do NOT use it for " +
    "company financials, and prefer the issuing agency when a question names " +
    "one.",
  keywords: [
    "inflation", "cpi", "unemployment", "gdp", "economy", "economic", "rate",
    "rates", "interest", "treasury", "yield", "fed", "federal", "reserve",
    "recession", "growth", "price", "prices", "index", "housing", "mortgage",
    "wage", "wages", "employment", "payrolls", "jobs", "money", "supply",
    "exchange", "currency", "oil", "energy", "production", "consumer",
    "spending", "deficit", "debt", "fred", "series", "statistic", "quarterly",
    "monthly", "seasonally", "adjusted", "print", "release", "published",
  ],
  paramsSchema: params,
  paramsHelp:
    "mode is required. observations and info need seriesId; search needs " +
    "query. If you do not know the series id, run mode=search first — guessing " +
    "one usually fails.",

  available: () => Boolean(process.env.FRED_API_KEY?.trim()),

  async run(input, signal) {
    if (input.mode === "search") return runSearch(input, signal);
    if (input.mode === "info") return runInfo(input, signal);
    return runObservations(input, signal);
  },
});
