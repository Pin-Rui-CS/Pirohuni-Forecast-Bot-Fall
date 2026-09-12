import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * Polymarket — real-money prediction markets.
 *
 * Tier B, emphatically. A market price is aggregated BELIEF, weighted by money
 * rather than by evidence. Quoting "the market says 73%" as though it settled a
 * question is the single most tempting error available to this agent, and it is
 * more tempting here than with Metaculus precisely because real money makes the
 * number feel authoritative.
 *
 * The one exception, encoded in the note: for a question that resolves ON
 * Polymarket, its own settled outcome is the resolution. That is a different
 * claim from its current price, and the `resolutionSource` field names what the
 * market itself will resolve against — often more useful than the price, since
 * it tells you which primary source to go and check.
 *
 * Verified 2026-09-08 via `/public-search`, which is the only text-search route
 * the gamma API offers; `/markets` filters by slug but has no query parameter.
 */

const params = z.object({
  query: z
    .string()
    .min(2)
    .describe('What to search for, e.g. "Ethiopia prime minister", "Fed rate cut".'),
  includeClosed: z
    .boolean()
    .optional()
    .describe("Include markets that have already resolved. Defaults to false."),
});

/** Prices and outcomes arrive as JSON-encoded STRINGS, not arrays. */
type Market = {
  question?: string;
  slug?: string;
  outcomes?: string;
  outcomePrices?: string;
  volumeNum?: number;
  liquidityNum?: number;
  endDate?: string;
  closed?: boolean;
  resolutionSource?: string;
  description?: string;
};

type Event = { title?: string; slug?: string; markets?: Market[] };
type Response = { events?: Event[] };

/** `'["Yes","No"]'` -> `["Yes","No"]`, tolerating anything that is not that. */
function jsonArray(raw: string | undefined): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

/** Pair each outcome with its price, as a percentage a reader can scan. */
function priced(market: Market): Array<{ outcome: string; probability: string }> {
  const outcomes = jsonArray(market.outcomes);
  const prices = jsonArray(market.outcomePrices);
  return outcomes.map((outcome, index) => {
    const price = Number(prices[index]);
    return {
      outcome,
      probability: Number.isFinite(price) ? `${(price * 100).toFixed(1)}%` : "unknown",
    };
  });
}

export const polymarket = defineAdapter({
  id: "polymarket",
  name: "Polymarket",
  tier: "B",
  domain: "forecasting",
  answers:
    "What real-money prediction markets currently price an outcome at, how much " +
    "money is behind it, when the market closes, and — often the most useful " +
    "field — what source the market will resolve against. Reach for it to " +
    "sanity-check your own estimate, to see how a question has been " +
    "operationalised, or to find which primary source settles it. NEVER cite a " +
    "price as evidence that something is true: it measures what traders " +
    "believe, not what is the case. A market at 95% is still a market at 95%.",
  keywords: [
    "prediction", "market", "markets", "odds", "probability", "betting", "bet",
    "traders", "polymarket", "forecast", "chance", "likelihood", "priced",
    "wager", "consensus", "crowd", "speculation",
  ],
  paramsSchema: params,
  paramsHelp:
    "query is required and matches market and event titles. Set includeClosed " +
    "to see markets that have already resolved.",

  async run(input, signal) {
    const url = new URL("https://gamma-api.polymarket.com/public-search");
    url.searchParams.set("q", input.query);
    url.searchParams.set("limit_per_type", "8");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "Polymarket",
      expect: (value) => isObject(value) && Array.isArray(value.events),
      expected: "an object with an events array",
    });

    const markets = (body.events ?? []).flatMap((event) =>
      (event.markets ?? [])
        .filter((market) => input.includeClosed || !market.closed)
        .map((market) => ({ event: event.title, market })),
    );

    const { rows, truncated } = capRows(
      markets.map(({ event, market }) => ({
        event: event ? truncate(event, 150) : undefined,
        question: truncate(market.question ?? "", 200),
        prices: priced(market),
        volumeUsd: market.volumeNum ? Math.round(market.volumeNum) : undefined,
        liquidityUsd: market.liquidityNum ? Math.round(market.liquidityNum) : undefined,
        closesAt: market.endDate,
        resolved: market.closed === true,
        // What the market itself will settle against. Frequently a better lead
        // than the price: it names the primary source worth checking.
        resolvesAgainst: market.resolutionSource
          ? truncate(market.resolutionSource, 200)
          : undefined,
        url: market.slug ? `https://polymarket.com/market/${market.slug}` : undefined,
      })),
    );

    return {
      tier: "B" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${markets.length} markets matched. ` +
        "THESE PRICES ARE BELIEFS, NOT FACTS. They tell you what traders are " +
        "willing to bet, which is evidence about opinion and nothing more. Do " +
        "not present a price as an answer, and do not convert one into a claim " +
        "about the world. Thin markets — low volumeUsd or liquidityUsd — carry " +
        "much less information than large ones. If a market names a " +
        "resolvesAgainst source, that source is worth checking directly: it is " +
        "usually Tier A where this is not.",
    };
  },
});
