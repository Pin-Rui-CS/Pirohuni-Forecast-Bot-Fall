import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, now } from "../http.ts";

/**
 * Coinbase Exchange candles.
 *
 * `api-registry-crossref.md` §1 is precise about why this is here rather than
 * an aggregator: "venue-specific intraday highs matter for 'at any point in
 * August' crypto wording; CoinGecko aggregation can miss a momentary print."
 * An aggregated index is a different number from a traded price on a named
 * venue, and questions are usually written against one or the other.
 *
 * Tier A for the venue's own prints, which is the only thing it claims to be.
 */

const params = z.object({
  product: z
    .string()
    .regex(/^[A-Za-z0-9]+-[A-Za-z0-9]+$/)
    .describe('Trading pair, e.g. "BTC-USD", "ETH-USD", "SOL-USD".'),
  granularity: z
    .enum(["60", "300", "900", "3600", "21600", "86400"])
    .describe(
      "Candle width in seconds: 60=1m, 300=5m, 900=15m, 3600=1h, " +
        "21600=6h, 86400=1d.",
    ),
  start: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("First day, YYYY-MM-DD. Omit both dates for the latest candles."),
  end: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Last day, YYYY-MM-DD."),
});

/** Coinbase returns positional arrays: [time, low, high, open, close, volume]. */
type Candle = [number, number, number, number, number, number];

export const coinbase = defineAdapter({
  id: "coinbase_candles",
  name: "Coinbase Exchange",
  tier: "A",
  domain: "markets",
  answers:
    "Historical crypto prices as traded on Coinbase: open, high, low, close " +
    "and volume per candle. Resolution-grade for 'did PAIR trade above X at " +
    "any point' because it gives the venue's actual intraday high, which an " +
    "aggregated index can miss. Do NOT use it for equities, FX or commodities, " +
    "and do NOT quote a Coinbase print when the question names a different " +
    "venue or an index.",
  keywords: [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency", "price",
    "coinbase", "candle", "ohlc", "high", "low", "close", "trading", "volume",
    "solana", "token", "coin", "exchange", "intraday", "dollar", "usd",
  ],
  paramsSchema: params,
  paramsHelp:
    "product and granularity are required. Coinbase returns at most 300 " +
    "candles per call, so pick a granularity that fits your window.",

  async run(input, signal) {
    const url = new URL(
      `https://api.exchange.coinbase.com/products/${input.product.toUpperCase()}/candles`,
    );
    url.searchParams.set("granularity", input.granularity);
    if (input.start) url.searchParams.set("start", `${input.start}T00:00:00Z`);
    if (input.end) url.searchParams.set("end", `${input.end}T23:59:59Z`);

    const href = url.toString();
    const candles = await getJson<Candle[]>(href, {
      signal,
      context: "Coinbase",
      expect: (value) =>
        Array.isArray(value) &&
        (value.length === 0 || (Array.isArray(value[0]) && value[0].length >= 6)),
      expected: "an array of [time, low, high, open, close, volume] arrays",
    });

    // Newest first from Coinbase; keep that order so a cap keeps recent data.
    const { rows, truncated } = capRows(
      candles.map(([time, low, high, open, close, volume]) => ({
        time: new Date(time * 1000).toISOString(),
        open,
        high,
        low,
        close,
        volume,
      })),
    );

    const highs = candles.map((candle) => candle[2]).filter(Number.isFinite);
    const lows = candles.map((candle) => candle[1]).filter(Number.isFinite);

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${candles.length} candles returned, newest first. ` +
        (highs.length
          ? `Range high ${Math.max(...highs)}, range low ${Math.min(...lows)}. `
          : "") +
        "These are Coinbase Exchange prints, not an index level and not a " +
        "futures price. A candle high is the highest TRADE within the candle.",
    };
  },
});
