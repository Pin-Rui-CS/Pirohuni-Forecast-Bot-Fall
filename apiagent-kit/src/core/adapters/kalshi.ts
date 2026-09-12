import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * Kalshi — the CFTC-regulated US prediction exchange.
 *
 * Not in `api-registry-crossref.md` at all, and confirmed open on 2026-09-08:
 * `/trade-api/v2` serves market data with no authentication. That matters
 * because Metaculus — the document's only forecasting source — has since closed
 * public access, leaving the domain empty.
 *
 * Tier B for prices, on exactly the same grounds as Polymarket: a price is
 * belief, not evidence. Kalshi differs in one respect worth encoding, though.
 * It is a regulated exchange, so each series publishes SETTLEMENT SOURCES — the
 * named primary sources it will resolve against — and a settled market is a
 * formal record of an outcome rather than an opinion about one.
 *
 * TWO-STEP BY NECESSITY. Kalshi has no text search (`/search` is a 404) and
 * `/events?with_nested_markets=true` is 3.7 MB per 200 events with more pages
 * behind it. So this caches the LIGHT event index (147 KB per 200, titles and
 * tickers only), filters it locally, then fetches markets for the best matches.
 */

const params = z.object({
  query: z
    .string()
    .min(2)
    .describe('Words to match against event titles, e.g. "Fed", "shutdown", "Mars".'),
  category: z
    .enum([
      "Politics",
      "Economics",
      "Financials",
      "Elections",
      "World",
      "Climate and Weather",
      "Companies",
      "Health",
      "Science and Technology",
      "Sports",
      "Entertainment",
      "Social",
    ])
    .optional()
    .describe("Narrow to one Kalshi category."),
});

type KalshiEvent = {
  event_ticker?: string;
  series_ticker?: string;
  title?: string;
  sub_title?: string;
  category?: string;
  settlement_sources?: Array<{ name?: string; url?: string }>;
};

type KalshiMarket = {
  ticker?: string;
  yes_sub_title?: string;
  last_price_dollars?: string | number | null;
  yes_bid_dollars?: string | number | null;
  yes_ask_dollars?: string | number | null;
  volume_fp?: string | number | null;
  open_interest_fp?: string | number | null;
  close_time?: string;
  status?: string;
  result?: string;
  rules_primary?: string;
};

const BASE = "https://api.elections.kalshi.com/trade-api/v2";

/** Pages of the light index to hold. 3 x 200 events ~ 450 KB. */
const INDEX_PAGES = 3;
const CACHE_MS = 15 * 60 * 1000;

let cached: { at: number; events: KalshiEvent[] } | null = null;

async function eventIndex(signal: AbortSignal): Promise<KalshiEvent[]> {
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.events;

  const events: KalshiEvent[] = [];
  let cursor: string | undefined;

  for (let page = 0; page < INDEX_PAGES; page += 1) {
    const url = new URL(`${BASE}/events`);
    url.searchParams.set("status", "open");
    url.searchParams.set("with_nested_markets", "false");
    url.searchParams.set("limit", "200");
    if (cursor) url.searchParams.set("cursor", cursor);

    const body = await getJson<{ events?: KalshiEvent[]; cursor?: string }>(
      url.toString(),
      {
        signal,
        context: "Kalshi events",
        expect: (value) => isObject(value) && Array.isArray(value.events),
        expected: "an object with an events array",
      },
    );

    events.push(...(body.events ?? []));
    cursor = body.cursor || undefined;
    if (!cursor) break;
  }

  cached = { at: Date.now(), events };
  return events;
}

/** Kalshi returns dollar figures as strings; "0.1200" must not become NaN. */
function money(value: string | number | null | undefined): number | undefined {
  if (value === null || value === undefined) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function percent(value: string | number | null | undefined): string | undefined {
  const parsed = money(value);
  return parsed === undefined ? undefined : `${(parsed * 100).toFixed(1)}%`;
}

export const kalshi = defineAdapter({
  id: "kalshi",
  name: "Kalshi",
  tier: "B",
  domain: "forecasting",
  answers:
    "Prices on the CFTC-regulated US prediction exchange, across politics, " +
    "economics, financials, weather, companies and sport. Also gives the " +
    "SETTLEMENT SOURCES each market resolves against and the exact resolution " +
    "rules, which are often more useful than the price because they name the " +
    "primary source that actually decides the question. NEVER quote a price as " +
    "evidence about the world — it is what traders believe. A market that has " +
    "already SETTLED is different: its result is a formal record.",
  keywords: [
    "kalshi", "prediction", "market", "markets", "odds", "probability",
    "betting", "contract", "traders", "exchange", "forecast", "chance",
    "likelihood", "settle", "settlement", "resolve", "shutdown", "cftc",
  ],
  paramsSchema: params,
  paramsHelp:
    "query is required and matches event titles only, so use broad words " +
    '("Fed", "shutdown") rather than a full question. category narrows it.',

  async run(input, signal) {
    const events = await eventIndex(signal);
    const words = input.query.toLowerCase().split(/\s+/).filter(Boolean);

    const matches = events
      .filter((event) => {
        if (input.category && event.category !== input.category) return false;
        const hay = `${event.title ?? ""} ${event.sub_title ?? ""}`.toLowerCase();
        return words.every((word) => hay.includes(word));
      })
      .slice(0, 4);

    if (matches.length === 0) {
      return {
        tier: "B" as const,
        url: `${BASE}/events?status=open`,
        retrievedAt: now(),
        rows: [],
        note:
          `No open Kalshi event title contains all of: ${words.join(", ")}. ` +
          `Searched ${events.length} open events. Try fewer or broader words — ` +
          "titles are short, so a full question will not match.",
      };
    }

    /*
     * Markets for the matching events only.
     *
     * Sequential rather than parallel: this is a regulated exchange being
     * queried anonymously, and four polite requests are worth more than a burst
     * that earns a rate limit.
     */
    const rows: unknown[] = [];
    for (const event of matches) {
      const url = new URL(`${BASE}/markets`);
      url.searchParams.set("event_ticker", event.event_ticker ?? "");
      url.searchParams.set("limit", "10");

      const body = await getJson<{ markets?: KalshiMarket[] }>(url.toString(), {
        signal,
        context: "Kalshi markets",
        expect: (value) => isObject(value) && Array.isArray(value.markets),
        expected: "an object with a markets array",
      });

      for (const market of body.markets ?? []) {
        rows.push({
          event: truncate(event.title ?? "", 150),
          category: event.category,
          outcome: market.yes_sub_title,
          yesPrice: percent(market.last_price_dollars),
          yesBid: percent(market.yes_bid_dollars),
          yesAsk: percent(market.yes_ask_dollars),
          volume: money(market.volume_fp),
          openInterest: money(market.open_interest_fp),
          closesAt: market.close_time,
          status: market.status,
          settled: market.result || undefined,
          // The named primary sources this market resolves against. Usually the
          // most valuable field here — it points at a Tier A source.
          settlesAgainst: (event.settlement_sources ?? [])
            .map((source) => source.name)
            .filter(Boolean),
          rules: market.rules_primary ? truncate(market.rules_primary, 300) : undefined,
          url: market.ticker
            ? `https://kalshi.com/markets/${event.series_ticker ?? ""}`
            : undefined,
        });
      }
    }

    const capped = capRows(rows);

    return {
      tier: "B" as const,
      url: `${BASE}/markets?event_ticker=${matches[0].event_ticker}`,
      retrievedAt: now(),
      rows: capped.rows,
      truncated: capped.truncated,
      note:
        `${matches.length} matching events, ${rows.length} markets. Prices are ` +
        "in probability terms (0.12 = 12%). THESE ARE BELIEFS, NOT FACTS: a " +
        "price says what traders will bet, not what is true, and a thin market " +
        "says very little. Two things here ARE stronger than the price: a " +
        "`settled` result is a formal record of an outcome, and " +
        "`settlesAgainst` names the primary sources the exchange itself will " +
        "defer to — check those directly rather than citing the price.",
    };
  },
});
