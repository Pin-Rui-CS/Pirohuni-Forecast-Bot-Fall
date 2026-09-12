import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, now, truncate } from "../http.ts";

/**
 * World Bank Open Data.
 *
 * The international counterpart to FRED, which is almost entirely US. One
 * adapter reaches every country and hundreds of indicators, which makes it the
 * largest single coverage gain available to this registry.
 *
 * Tier A with the same mirror caveat FRED carries, and for the same reason: the
 * World Bank COMPILES national statistics it does not produce. GDP for Singapore
 * originates with SingStat; the World Bank harmonises and republishes it. For a
 * question naming a national statistics office, this is a mirror of that office
 * and the office is the resolution source.
 *
 * The trap specific to this API is lag. Annual indicators for the year just
 * ended usually do not exist yet, and the response says so only by omitting the
 * row — so `mode=info` reports the newest year that actually has a value, which
 * is the same temporal-feasibility gate FRED's `info` mode provides.
 */

const params = z.object({
  mode: z
    .enum(["observations", "search"])
    .describe(
      "observations = the values for a country and indicator. " +
        "search = find an indicator code from a description.",
    ),
  country: z
    .string()
    .regex(/^[A-Za-z]{2,3}$/)
    .optional()
    .describe(
      'ISO country code, 2 or 3 letters — "SGP" or "SG", "USA", "GBR". ' +
        'Use "WLD" for the world aggregate. Required for observations.',
    ),
  indicator: z
    .string()
    .regex(/^[A-Za-z0-9._-]{3,40}$/)
    .optional()
    .describe(
      "World Bank indicator code. Common ones: NY.GDP.MKTP.CD (GDP current " +
        "US$), NY.GDP.PCAP.CD (GDP per capita), SP.POP.TOTL (population), " +
        "FP.CPI.TOTL.ZG (inflation %), SL.UEM.TOTL.ZS (unemployment %), " +
        "EN.GHG.CO2.MT.CE.AR5 (CO2 emissions). Required for observations.",
    ),
  query: z.string().optional().describe("For mode=search, e.g. \"life expectancy\"."),
  from: z
    .string()
    .regex(/^\d{4}$/)
    .optional()
    .describe("First year, e.g. 2015. Defaults to ten years back."),
  to: z.string().regex(/^\d{4}$/).optional().describe("Last year, e.g. 2024."),
});

/** Every World Bank response is [meta, rows] — an array, not an object. */
type Envelope<T> = [{ total?: number; lastupdated?: string; message?: unknown }, T[] | null];

type Observation = {
  indicator?: { id?: string; value?: string };
  country?: { id?: string; value?: string };
  countryiso3code?: string;
  date?: string;
  value?: number | null;
};

type Indicator = {
  id?: string;
  name?: string;
  sourceNote?: string;
  unit?: string;
};

const BASE = "https://api.worldbank.org/v2";

/**
 * The envelope is a two-element array whose second element is the payload, and
 * is `null` when nothing matched. An error is reported as a `message` key on
 * the first element WITH a 200 status, so shape checking has to look at both.
 */
function envelopeOk(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === "object" &&
    value[0] !== null &&
    !("message" in (value[0] as object))
  );
}

export const worldBank = defineAdapter({
  id: "world_bank",
  name: "World Bank Open Data",
  tier: "A",
  domain: "statistics",
  answers:
    "International statistics for any country: GDP, population, inflation, " +
    "unemployment, life expectancy, emissions, trade, education, poverty — " +
    "hundreds of indicators, most going back decades. This is the source to " +
    "reach for when a question is about a country OTHER than the United " +
    "States, or compares countries. Use mode=search when you do not know the " +
    "indicator code. Do NOT use it for anything monthly or daily: almost " +
    "everything here is ANNUAL and lags by a year or more.",
  keywords: [
    "country", "countries", "gdp", "population", "inflation", "unemployment",
    "poverty", "emissions", "co2", "trade", "exports", "imports", "income",
    "life", "expectancy", "literacy", "education", "development", "economy",
    "international", "global", "world", "bank", "national", "per", "capita",
    "growth", "statistics", "indicator", "singapore", "china", "india",
  ],
  paramsSchema: params,
  paramsHelp:
    "mode is required. observations needs country and indicator; search needs " +
    "query. Data is annual — from/to are 4-digit years, not full dates.",

  async run(input, signal) {
    if (input.mode === "search") {
      if (!input.query) throw new Error("query is required when mode is search.");

      const url = new URL(`${BASE}/indicator`);
      url.searchParams.set("format", "json");
      url.searchParams.set("per_page", "20");
      url.searchParams.set("source", "2");
      const href = url.toString();

      // The indicator list has no server-side text search, so match locally
      // over one page of the most common source (World Development Indicators).
      const body = await getJson<Envelope<Indicator>>(
        `${href}&per_page=500`,
        {
          signal,
          context: "World Bank indicators",
          expect: envelopeOk,
          expected: "a [meta, rows] envelope",
        },
      );

      const needle = input.query.toLowerCase();
      const hits = (body[1] ?? []).filter(
        (indicator) =>
          (indicator.name ?? "").toLowerCase().includes(needle) ||
          (indicator.id ?? "").toLowerCase().includes(needle),
      );

      const { rows, truncated } = capRows(
        hits.map((indicator) => ({
          indicator: indicator.id,
          name: indicator.name,
          unit: indicator.unit || undefined,
          about: indicator.sourceNote ? truncate(indicator.sourceNote, 300) : undefined,
        })),
      );

      return {
        tier: "A" as const,
        url: `${href}&per_page=500`,
        retrievedAt: now(),
        rows,
        truncated,
        note:
          rows.length === 0
            ? "No indicator matched. Try a broader word — the names are formal, " +
              'e.g. "GDP per capita" rather than "how rich".'
            : "Codes only. Call again with mode=observations and one of these.",
      };
    }

    if (!input.country || !input.indicator) {
      throw new Error("country and indicator are both required for observations.");
    }

    const thisYear = new Date().getUTCFullYear();
    const from = input.from ?? String(thisYear - 10);
    const to = input.to ?? String(thisYear);

    const url = new URL(
      `${BASE}/country/${input.country.toUpperCase()}/indicator/${input.indicator.toUpperCase()}`,
    );
    url.searchParams.set("format", "json");
    url.searchParams.set("date", `${from}:${to}`);
    url.searchParams.set("per_page", "100");

    const href = url.toString();
    const body = await getJson<Envelope<Observation>>(href, {
      signal,
      context: "World Bank",
      expect: envelopeOk,
      expected: "a [meta, rows] envelope",
    });

    const all = body[1] ?? [];
    // A null value means the year exists in the series but has no figure — a
    // different thing from the year being absent, and it must not become a zero.
    const withValues = all.filter((row) => typeof row.value === "number");
    const missing = all.length - withValues.length;

    const { rows, truncated } = capRows(
      withValues.map((row) => ({
        year: row.date,
        value: row.value,
        country: row.country?.value,
        indicator: row.indicator?.value,
      })),
    );

    const latest = withValues
      .map((row) => row.date)
      .filter((year): year is string => Boolean(year))
      .sort()
      .at(-1);

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        (rows.length === 0
          ? "No values in this range — the indicator may not be reported for " +
            "this country, or the years may be too recent. "
          : `Newest year WITH a value is ${latest}. Anything after that has not ` +
            "been published; annual indicators routinely lag by a year or more. ") +
        (missing > 0 ? `${missing} years in range had no figure. ` : "") +
        (body[0]?.lastupdated ? `Series last updated ${body[0].lastupdated}. ` : "") +
        "The World Bank compiles national statistics it does not produce. If " +
        "the question names a country's own statistics office, this is a mirror " +
        "and that office is the resolution source.",
    };
  },
});
