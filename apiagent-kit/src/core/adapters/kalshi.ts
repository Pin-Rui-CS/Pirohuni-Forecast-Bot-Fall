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
 * TWO-STEP. The trade API has no text search (`/trade-api/v2/search` is a
 * 404), but kalshi.com's own search box is served by `/v1/search/series`,
 * which ranks open events for a query. This asks it first, then fetches each
 * hit's event with its markets from the documented API. It is undocumented,
 * so on any failure this falls back to the LIGHT event index (titles and
 * tickers only, 147 KB per 200 events) filtered locally.
 *
 * Why the search (2026-11-09): Kalshi lists ~18,300 open events and the index
 * holds the first 600, so it could not see the Cyber Monday spend market on
 * the exact question it was asked about, nor "Best AI at the end of 2026";
 * the search returns each as its first hit. ("Iran" matched 0 of 600.)
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
const SEARCH = "https://api.elections.kalshi.com/v1/search/series";
const MAX_EVENTS = 4;

type SearchHit = {
  event_ticker?: string;
  event_title?: string;
  event_subtitle?: string;
  series_title?: string;
  category?: string;
  active_market_count?: number;
};

/**
 * Open events kalshi.com's search returns for the query, best first; null
 * when the search is unavailable (the caller then filters the index).
 */
async function searchEvents(
  query: string,
  category: string | undefined,
  signal: AbortSignal,
): Promise<string[] | null> {
  const url = new URL(SEARCH);
  url.searchParams.set("query", query);
  try {
    const body = await getJson<{ current_page?: SearchHit[] }>(url.toString(), {
      signal,
      context: "Kalshi search",
      expect: (value) => isObject(value) && Array.isArray(value.current_page),
      expected: "an object with a current_page array",
    });
    // The search is fuzzy ("Pantone" returns Texas Senate races), and nothing
    // after this step judges relevance, so a hit must name a query word.
    const words = query.toLowerCase().split(/\W+/).filter((word) => word.length >= 3);
    return (body.current_page ?? [])
      .filter((hit) => (hit.active_market_count ?? 0) > 0)
      .filter((hit) => !category || hit.category === category)
      .filter((hit) => {
        const hay = `${hit.event_title ?? ""} ${hit.event_subtitle ?? ""} ${hit.series_title ?? ""}`.toLowerCase();
        // Whole words: "cyber" must not match "Cybersecurity". Words split on
        // \W hold only [a-z0-9_], so they are safe inside a RegExp.
        return words.length === 0 || words.some((word) => new RegExp(`\\b${word}\\b`).test(hay));
      })
      .map((hit) => hit.event_ticker ?? "")
      .filter((ticker, i, all) => ticker && all.indexOf(ticker) === i)
      .slice(0, MAX_EVENTS);
  } catch {
    return null;
  }
}

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
    const words = input.query.toLowerCase().split(/\s+/).filter(Boolean);
    let tickers = await searchEvents(input.query, input.category, signal);
    let searched = `kalshi.com search for "${input.query}"`;

    if (tickers === null) {
      const events = await eventIndex(signal);
      searched = `${events.length} open event titles (kalshi.com search unavailable)`;
      tickers = events
        .filter((event) => {
          if (input.category && event.category !== input.category) return false;
          const hay = `${event.title ?? ""} ${event.sub_title ?? ""}`.toLowerCase();
          return words.every((word) => hay.includes(word));
        })
        .map((event) => event.event_ticker ?? "")
        .filter(Boolean)
        .slice(0, MAX_EVENTS);
    }

    if (tickers.length === 0) {
      return {
        tier: "B" as const,
        url: `${SEARCH}?query=${encodeURIComponent(input.query)}`,
        retrievedAt: now(),
        rows: [],
        note:
          `No open Kalshi event found for: ${words.join(", ")} (searched ${searched}). ` +
          "Try fewer or broader words.",
      };
    }

    /*
     * Each matching event with its markets, from the documented API.
     *
     * Sequential rather than parallel: this is a regulated exchange being
     * queried anonymously, and four polite requests are worth more than a burst
     * that earns a rate limit.
     */
    const rows: unknown[] = [];
    for (const ticker of tickers) {
      const url = new URL(`${BASE}/events/${encodeURIComponent(ticker)}`);
      url.searchParams.set("with_nested_markets", "true");

      const body = await getJson<{ event?: KalshiEvent & { markets?: KalshiMarket[] } }>(url.toString(), {
        signal,
        context: "Kalshi event",
        expect: (value) => isObject(value) && isObject(value.event),
        expected: "an object with an event",
      });
      const event = body.event ?? {};

      for (const market of (event.markets ?? []).slice(0, 10)) {
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
      url: `${BASE}/events/${tickers[0]}?with_nested_markets=true`,
      retrievedAt: now(),
      rows: capped.rows,
      truncated: capped.truncated,
      note:
        `${tickers.length} matching events (${searched}), ${rows.length} markets. Prices are ` +
        "in probability terms (0.12 = 12%). THESE ARE BELIEFS, NOT FACTS: a " +
        "price says what traders will bet, not what is true, and a thin market " +
        "says very little. Two things here ARE stronger than the price: a " +
        "`settled` result is a formal record of an outcome, and " +
        "`settlesAgainst` names the primary sources the exchange itself will " +
        "defer to — check those directly rather than citing the price.",
    };
  },
});
