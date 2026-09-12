import { z } from "zod";
import { defineAdapter } from "../types.ts";
import {
  capRows,
  getText,
  now,
  spaced,
  truncate,
  HttpError,
  ShapeError,
} from "../http.ts";

/**
 * GDELT DOC 2.0 — worldwide news article index.
 *
 * `api-registry-crossref.md` §1 calls this "my largest single omission",
 * covering roughly thirty event-detection questions that had no discovery layer
 * at all. It is also the document's clearest example of the tier distinction:
 * GDELT tells you an event was REPORTED, never that it HAPPENED. Tier B, and
 * the description below says so to the model in as many words.
 *
 * It is additionally the reason `http.ts` checks shape rather than status:
 * GDELT answers a malformed query with HTTP 200 and a plain-text or HTML error
 * body. Parsing that as JSON is exactly the §3 failure mode.
 *
 * MEASURED 2026-09-06, and the crossref does not mention any of it:
 *
 *  - The published limit is one request every five seconds, enforced per IP
 *    with a 429. Fanning `call_api` out in parallel — which the rest of this
 *    project encourages, because parallel calls in one step cost one gateway
 *    request — produces five refusals and one answer. Hence `spaced()`.
 *  - A refusal takes 12-13 SECONDS to arrive. Under the default ten-second
 *    ceiling that surfaces as an unexplained timeout rather than as the
 *    actionable "you are going too fast" that it is. Hence the raised timeout.
 *  - The real limit is stricter than the published one, or the penalty for
 *    breaking it is long. After a burst of test calls, a 45-second gap was
 *    served but two subsequent 30-second gaps were both refused. Three data
 *    points taken straight after repeated violations is not enough to call the
 *    true shape of the window, so treat the spacing below as a floor rather
 *    than a guarantee — the 429 path is what actually keeps a turn alive.
 */

/**
 * Well above the published five seconds, because five is demonstrably not
 * enough from a single address. Two GDELT calls in a turn cost thirty seconds
 * of a 300-second budget, which is affordable; being throttled into a useless
 * result is not.
 */
const MIN_INTERVAL_MS = 15_000;

/** A refusal alone takes 12-13s, so the ceiling has to clear that to be able to
 *  READ the refusal. Measured, not guessed. */
const TIMEOUT_MS = 30_000;

const params = z.object({
  query: z
    .string()
    .min(2)
    .describe(
      'Search terms. Phrases go in double quotes. GDELT operators work: ' +
        'sourcelang:english, domain:reuters.com, sourcecountry:US.',
    ),
  timespan: z
    .string()
    .regex(/^\d+[hdwm]$/)
    .optional()
    .describe('Window back from now: "24h", "7d", "3w", "1m". Defaults to 7d.'),
  startDate: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe(
      "YYYY-MM-DD. Start of a fixed window, used instead of timespan. " +
        "endDate may be omitted, in which case the window runs up to today.",
    ),
  endDate: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("YYYY-MM-DD, inclusive. Requires startDate."),
})
  /*
   * endDate without startDate is rejected rather than quietly reinterpreted.
   *
   * This whole object used to require BOTH dates before it would use a window,
   * so a call carrying only startDate silently fell through to the default
   * seven-day timespan. Measured 2026-09-06: asked for coverage since
   * 2026-08-06, the adapter searched seven days, and the model reported "no
   * results in the requested timeframe (starting from August 6)" — a confident
   * answer about a month it never looked at.
   *
   * That is the §3 failure wearing different clothes: not a bad response
   * believed, but a request silently altered. An adapter must honour its
   * parameters or refuse them, never split the difference.
   */
  .refine((input) => !input.endDate || input.startDate, {
    message: "endDate requires startDate.",
    path: ["endDate"],
  });

type Params = z.infer<typeof params>;

type Article = {
  url?: string;
  title?: string;
  seendate?: string;
  domain?: string;
  language?: string;
  sourcecountry?: string;
};

/** All GDELT calls share one gate — the limit is per address, not per query. */
const GATE = "gdelt";

/** GDELT wants YYYYMMDDHHMMSS. */
function stamp(date: string, endOfDay: boolean): string {
  return `${date.replace(/-/g, "")}${endOfDay ? "235959" : "000000"}`;
}

function build(input: Params): string {
  const url = new URL("https://api.gdeltproject.org/api/v2/doc/doc");
  url.searchParams.set("query", input.query);
  url.searchParams.set("mode", "artlist");
  url.searchParams.set("format", "json");
  url.searchParams.set("maxrecords", "25");
  url.searchParams.set("sort", "datedesc");

  // A startDate is enough to mean "window", with today as the open end. The
  // schema has already guaranteed endDate never arrives on its own.
  if (input.startDate) {
    url.searchParams.set("startdatetime", stamp(input.startDate, false));
    url.searchParams.set(
      "enddatetime",
      stamp(input.endDate ?? new Date().toISOString().slice(0, 10), true),
    );
  } else {
    url.searchParams.set("timespan", input.timespan ?? "7d");
  }

  return url.toString();
}

/** What was actually searched, so the model cannot mistake the window. */
function windowOf(input: Params): string {
  if (input.startDate) {
    return `${input.startDate} to ${input.endDate ?? "today"}`;
  }
  return `the last ${input.timespan ?? "7d"}`;
}

export const gdelt = defineAdapter({
  id: "gdelt_doc",
  name: "GDELT DOC 2.0",
  tier: "B",
  domain: "events",
  answers:
    "Whether an event has been REPORTED in worldwide news, and when reporting " +
    "first appeared. Reach for it for meetings, calls, visits, strikes, " +
    "ceasefires, resignations, announcements — anything whose answer is 'did " +
    "this happen yet'. It is a DISCOVERY layer: it establishes that coverage " +
    "exists and points at the articles. Do NOT cite it as the source that " +
    "settles a question, and do NOT treat an article count as a measurement of " +
    "anything.",
  keywords: [
    "news", "event", "reported", "happened", "announcement", "meeting", "call",
    "visit", "summit", "ceasefire", "strike", "protest", "resign", "election",
    "attack", "war", "sanctions", "treaty", "coverage", "media", "article",
    "recently", "did", "occur", "diplomatic", "talks", "agreement",
  ],
  paramsSchema: params,
  paramsHelp:
    'query is required, e.g. \'"Putin" "Trump" call\'. Either give timespan ' +
    '("7d") or both startDate and endDate as YYYY-MM-DD.',

  async run(input, signal) {
    const url = build(input);

    let text: string;
    try {
      text = await spaced(GATE, MIN_INTERVAL_MS, () =>
        getText(url, { signal, context: "GDELT", timeoutMs: TIMEOUT_MS }),
      );
    } catch (error) {
      /*
       * Being throttled is information, not a crash.
       *
       * Returning it as a normal result with zero rows lets the model say "I
       * could not check the news, here is what the other sources gave me",
       * which is a far better turn than a dead tool call — and it keeps the
       * turn's remaining steps for work that can still succeed.
       */
      if (error instanceof HttpError && error.status === 429) {
        return {
          tier: "B" as const,
          url,
          retrievedAt: now(),
          rows: [],
          note:
            "GDELT REFUSED THIS REQUEST — it is rate limiting this address. " +
            "No search was performed. This is NOT evidence that no coverage " +
            "exists; say the check could not be run rather than reporting an " +
            "absence. Do not retry it this turn.",
        };
      }
      throw error;
    }

    // GDELT answers bad queries with 200 and a non-JSON body. Everything below
    // depends on catching that here rather than downstream.
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      const message = text.trim().slice(0, 200);
      throw new ShapeError(
        "GDELT",
        "JSON with an articles array",
        message || "an empty body",
      );
    }

    // An empty result set is legitimately `{}` — no `articles` key at all — so
    // a missing key is not an error, but a present non-array one is.
    const articles = (parsed as { articles?: unknown }).articles;
    if (articles !== undefined && !Array.isArray(articles)) {
      throw new ShapeError("GDELT", "articles to be an array", typeof articles);
    }

    const list = (articles ?? []) as Article[];
    const { rows, truncated } = capRows(
      list.flatMap((article) =>
        article.url
          ? [
              {
                title: truncate(article.title ?? article.url, 300),
                url: article.url,
                seenAt: article.seendate,
                domain: article.domain,
                country: article.sourcecountry,
              },
            ]
          : [],
      ),
    );

    return {
      tier: "B" as const,
      url,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `Searched ${windowOf(input)}. ` +
        (rows.length === 0
          ? "No articles matched in that window — check it is the window you " +
            "meant before concluding anything. "
          : "") +
        "Reported coverage, not confirmation. GDELT indexes what news outlets " +
        "published; it does not verify that the event occurred. Never cite as " +
        "a resolution source.",
    };
  },
});
