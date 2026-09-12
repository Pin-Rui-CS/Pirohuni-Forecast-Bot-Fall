import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * SEC EDGAR — company filings.
 *
 * Tier A. A filing is a legal act with a timestamp, so "has company X filed a
 * Y" is settled here rather than reported.
 *
 * Two operational notes. EDGAR requires a descriptive User-Agent identifying
 * the caller and returns 403 without one — `http.ts` sets it, and
 * `SEC_EDGAR_CONTACT` adds a contact address if you choose to supply one. And
 * the submissions endpoint takes a zero-padded ten-digit CIK, not a ticker, so
 * a ticker is resolved through the published mapping file first.
 */

const params = z.object({
  ticker: z
    .string()
    .optional()
    .describe('Stock ticker, e.g. "AAPL". Either this or cik is required.'),
  cik: z
    .string()
    .regex(/^\d{1,10}$/)
    .optional()
    .describe("SEC CIK number. Padding is handled for you."),
  form: z
    .string()
    .optional()
    .describe('Filter to one form type, e.g. "8-K", "10-Q", "10-K", "4", "13F-HR".'),
  since: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Only filings on or after this date, YYYY-MM-DD."),
});

type TickerMap = Record<string, { cik_str?: number; ticker?: string; title?: string }>;

type Submissions = {
  cik?: string;
  name?: string;
  filings?: {
    recent?: {
      accessionNumber?: string[];
      filingDate?: string[];
      reportDate?: string[];
      form?: string[];
      primaryDocument?: string[];
      primaryDocDescription?: string[];
    };
  };
};

/**
 * Ticker to CIK, cached per instance.
 *
 * The mapping file is ~1 MB and changes rarely; downloading it once per cold
 * start is much cheaper than once per question.
 */
let tickerCache: { at: number; map: Map<string, number> } | null = null;
const CACHE_MS = 12 * 60 * 60 * 1000;

async function resolveTicker(
  ticker: string,
  signal: AbortSignal,
): Promise<number | undefined> {
  if (!tickerCache || Date.now() - tickerCache.at > CACHE_MS) {
    const raw = await getJson<TickerMap>(
      "https://www.sec.gov/files/company_tickers.json",
      {
        signal,
        context: "SEC ticker map",
        expect: (value) => isObject(value) && Object.keys(value).length > 0,
        expected: "an object of ticker records",
      },
    );
    const map = new Map<string, number>();
    for (const entry of Object.values(raw)) {
      if (entry?.ticker && typeof entry.cik_str === "number") {
        map.set(entry.ticker.toUpperCase(), entry.cik_str);
      }
    }
    tickerCache = { at: Date.now(), map };
  }
  return tickerCache.map.get(ticker.toUpperCase());
}

export const secEdgar = defineAdapter({
  id: "sec_edgar",
  name: "SEC EDGAR",
  tier: "A",
  domain: "filings",
  answers:
    "What a US-listed company has filed with the SEC and when: 8-K material " +
    "events, 10-K and 10-Q reports, Form 4 insider trades, S-1 registrations. " +
    "Resolution-grade for 'will COMPANY file FORM by DATE' and for " +
    "establishing the date a disclosure became public. Do NOT use it for " +
    "share prices, for private companies, or for the CONTENTS of a filing " +
    "beyond its type and date.",
  keywords: [
    "sec", "edgar", "filing", "filings", "company", "10-k", "10-q", "8-k",
    "annual", "quarterly", "report", "insider", "disclosure", "earnings",
    "registration", "ipo", "s-1", "shares", "stock", "corporate", "public",
    "ticker", "material", "prospectus",
  ],
  paramsSchema: params,
  paramsHelp:
    "Give ticker or cik. form narrows to one filing type; since narrows by " +
    "date. Returns the most recent filings first.",

  async run(input, signal) {
    let cik: number | undefined;

    if (input.cik) {
      cik = Number(input.cik);
    } else if (input.ticker) {
      cik = await resolveTicker(input.ticker, signal);
      if (cik === undefined) {
        return {
          tier: "A" as const,
          url: "https://www.sec.gov/files/company_tickers.json",
          retrievedAt: now(),
          rows: [],
          note:
            `No SEC registrant matches the ticker "${input.ticker}". It may be ` +
            "foreign-listed, private, or delisted. Try the cik parameter.",
        };
      }
    } else {
      throw new Error("Either ticker or cik is required.");
    }

    const padded = String(cik).padStart(10, "0");
    const href = `https://data.sec.gov/submissions/CIK${padded}.json`;

    const body = await getJson<Submissions>(href, {
      signal,
      context: "SEC EDGAR",
      expect: (value) => isObject(value) && isObject(value.filings),
      expected: "an object with a filings section",
    });

    /*
     * EDGAR returns column arrays, not row objects: form[i], filingDate[i] and
     * accessionNumber[i] describe one filing between them. Zip before anything
     * else, because every filter below is per-filing.
     */
    const recent = body.filings?.recent ?? {};
    const count = recent.accessionNumber?.length ?? 0;
    let filings = Array.from({ length: count }, (_, i) => ({
      form: recent.form?.[i],
      filedOn: recent.filingDate?.[i],
      reportDate: recent.reportDate?.[i] || undefined,
      description: recent.primaryDocDescription?.[i] || undefined,
      accession: recent.accessionNumber?.[i],
    }));

    if (input.form) {
      const wanted = input.form.toUpperCase();
      filings = filings.filter((filing) => filing.form?.toUpperCase() === wanted);
    }
    if (input.since) {
      filings = filings.filter((filing) => (filing.filedOn ?? "") >= input.since!);
    }

    const matched = filings.length;
    const { rows, truncated } = capRows(
      filings.map((filing) => ({
        ...filing,
        url: filing.accession
          ? `https://www.sec.gov/Archives/edgar/data/${cik}/` +
            `${filing.accession.replace(/-/g, "")}/${filing.accession}-index.htm`
          : undefined,
      })),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${truncate(body.name ?? "Unknown", 120)} (CIK ${cik}). ` +
        `${matched} filings matched out of the ${count} most recent. ` +
        "This endpoint covers roughly the last year plus the current one; " +
        "older filings live in separate archive files.",
    };
  },
});
