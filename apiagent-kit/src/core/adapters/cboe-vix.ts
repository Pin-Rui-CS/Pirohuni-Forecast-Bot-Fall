import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, csvToObjects, getText, now, ShapeError } from "../http.ts";

/**
 * Cboe VIX daily history.
 *
 * `api-registry-crossref.md` §1 prefers this to Yahoo for closes on provenance
 * grounds — it is Cboe's own published file — while noting it is daily only and
 * not intraday. Both halves are true and both are enforced below: the file
 * carries OPEN/HIGH/LOW/CLOSE per session and nothing finer.
 *
 * Tier A for the index level, and the note is emphatic about what that is NOT.
 * §6 lists "index ≠ contract" among the confusions running through roughly
 * fifteen of the market questions, and VIX is where that bites hardest: the VIX
 * index is a calculated statistic that cannot be traded, VIX futures settle
 * against a separate auction, and VIX options are priced off the futures. A
 * question about "VIX at 20" usually means the index; a question about a
 * position in VIX never does.
 *
 * Verified 2026-09-06: 472 KB, daily rows back to 1990-01-02, current through
 * the most recent session.
 */

const params = z.object({
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Earliest session date, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Latest session date, YYYY-MM-DD."),
});

const SOURCE =
  "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv";

/** Whole-file download cached per instance, like the CISA KEV catalogue. It
 *  changes once a day at most and is half a megabyte. */
let cached: { at: number; rows: Array<Record<string, string>> } | null = null;
const CACHE_MS = 60 * 60 * 1000;

/** Cboe publishes MM/DD/YYYY; everything else in this project is ISO. */
function toIso(american: string): string | null {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(american.trim());
  return match ? `${match[3]}-${match[1]}-${match[2]}` : null;
}

async function load(signal: AbortSignal): Promise<Array<Record<string, string>>> {
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.rows;

  const text = await getText(SOURCE, { signal, context: "Cboe VIX", timeoutMs: 20_000 });

  // The header is the contract. A redirect to an error page or a changed export
  // format both land here rather than silently yielding zero sessions.
  if (!text.startsWith("DATE,OPEN,HIGH,LOW,CLOSE")) {
    throw new ShapeError(
      "Cboe VIX",
      "a CSV headed DATE,OPEN,HIGH,LOW,CLOSE",
      text.slice(0, 200),
    );
  }

  const rows = csvToObjects(text);
  cached = { at: Date.now(), rows };
  return rows;
}

export const cboeVix = defineAdapter({
  id: "cboe_vix",
  name: "Cboe VIX history",
  tier: "A",
  domain: "markets",
  answers:
    "Daily VIX index levels — open, high, low and close per trading session, " +
    "back to 1990, from Cboe's own published file. Resolution-grade for 'will " +
    "the VIX close above N' and for the highest or lowest level over a window. " +
    "This is the INDEX, not VIX futures and not a tradeable instrument. It is " +
    "daily only: it cannot answer a question about a specific time of day. Do " +
    "NOT use it for equity prices or for other volatility measures.",
  keywords: [
    "vix", "volatility", "fear", "index", "cboe", "market", "spike", "close",
    "stocks", "equity", "risk", "turbulence", "panic", "spx", "options",
  ],
  paramsSchema: params,
  paramsHelp:
    "from and to are both required and inclusive, YYYY-MM-DD. Non-trading days " +
    "simply have no row.",

  async run(input, signal) {
    const all = await load(signal);

    const sessions = all.flatMap((row) => {
      const date = toIso(row.DATE ?? "");
      if (!date || date < input.from || date > input.to) return [];
      return [
        {
          date,
          open: Number(row.OPEN),
          high: Number(row.HIGH),
          low: Number(row.LOW),
          close: Number(row.CLOSE),
        },
      ];
    });

    // Newest first, so a cap keeps the end of the window nearest the question.
    sessions.reverse();
    const matched = sessions.length;
    const { rows, truncated } = capRows(sessions);

    const highs = sessions.map((s) => s.high).filter(Number.isFinite);
    const lows = sessions.map((s) => s.low).filter(Number.isFinite);
    const closes = sessions.map((s) => s.close).filter(Number.isFinite);

    return {
      tier: "A" as const,
      url: SOURCE,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${matched} trading sessions in range` +
        (highs.length
          ? `; period high ${Math.max(...highs)}, low ${Math.min(...lows)}, ` +
            `highest close ${Math.max(...closes)}`
          : "") +
        ". These are VIX INDEX levels. The index is a calculated statistic and " +
        "cannot be traded: VIX futures settle against a separate auction and " +
        "options are priced off the futures, so a level here does not answer a " +
        "question about a VIX position. Daily only — there is no intraday " +
        "series in this file, so 'at any point during the day' is answerable " +
        "only as far as the session high.",
    };
  },
});
