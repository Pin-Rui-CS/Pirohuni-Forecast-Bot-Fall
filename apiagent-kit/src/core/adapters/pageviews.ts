import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, HttpError, isObject, now } from "../http.ts";

/**
 * Wikimedia Pageviews — daily traffic to a Wikipedia article.
 *
 * This adapter exists because it adds a KIND of signal nothing else in the
 * registry has. Everything else here measures what happened; this measures what
 * people are looking at, and attention frequently moves BEFORE the event a
 * question is about — searches for a disease climb before the case counts are
 * published, for a company before the filing, for a politician before the
 * resignation.
 *
 * TIER, CAREFULLY. Marked A, and the boundary of that claim is narrow enough to
 * be worth stating twice. The number is Wikimedia counting its own traffic, so
 * it is a fact ABOUT ATTENTION. Any conclusion drawn from it about the world is
 * inference, and a weak one: attention spikes on rumour, on anniversaries, on a
 * television mention, and on nothing at all. The note below refuses that leap
 * explicitly, because an adapter that hands a model a rising line without
 * saying what it is not would become a confabulation engine.
 */

const params = z.object({
  article: z
    .string()
    .min(1)
    .describe(
      'Exact Wikipedia article title, spaces allowed, e.g. "Ebola" or ' +
        '"2026 Atlantic hurricane season". Case-sensitive after the first letter.',
    ),
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("First day, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Last day, YYYY-MM-DD."),
  project: z
    .string()
    .regex(/^[a-z-]{2,12}\.wikipedia$/)
    .optional()
    .describe('Language edition, defaults to "en.wikipedia".'),
});

type Item = { timestamp?: string; views?: number };
type Response = { items?: Item[] };

/** The API wants YYYYMMDD, and the article title percent-encoded with spaces
 *  as underscores — a raw space or slash breaks the path segment. */
function slug(article: string): string {
  return encodeURIComponent(article.trim().replace(/\s+/g, "_"));
}

/** `2026080100` -> `2026-08-01`. */
function toIso(stamp: string | undefined): string | undefined {
  if (!stamp || stamp.length < 8) return undefined;
  return `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}`;
}

export const pageviews = defineAdapter({
  id: "wikipedia_pageviews",
  name: "Wikimedia Pageviews",
  tier: "A",
  domain: "events",
  answers:
    "Daily Wikipedia pageviews for an article — a measure of PUBLIC ATTENTION " +
    "over time. Reach for it to detect when interest in a topic spiked, to " +
    "compare attention between periods, or as an early signal that something " +
    "happened before official data catches up. It measures attention and " +
    "NOTHING ELSE: it cannot tell you an event occurred, how many people were " +
    "affected, or what anyone thinks. Do NOT use it as evidence for any factual " +
    "claim about the world.",
  keywords: [
    "attention", "interest", "views", "pageviews", "traffic", "popularity",
    "trending", "spike", "searches", "public", "buzz", "awareness", "surge",
    "wikipedia", "readers", "curiosity",
  ],
  paramsSchema: params,
  paramsHelp:
    "article, from and to are required. Use the exact Wikipedia title — if you " +
    "are unsure of it, look the article up with the wikipedia tool first.",

  async run(input, signal) {
    const project = input.project ?? "en.wikipedia";
    const start = input.from.replace(/-/g, "");
    const end = input.to.replace(/-/g, "");

    const href =
      "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/" +
      `${project}/all-access/user/${slug(input.article)}/daily/${start}/${end}`;

    let body: Response;
    try {
      body = await getJson<Response>(href, {
        signal,
        context: "Wikimedia Pageviews",
        expect: (value) => isObject(value) && Array.isArray(value.items),
        expected: "an object with an items array",
      });
    } catch (error) {
      // A title with no data returns 404. That is "no such article, or no
      // traffic in this window" — an answer, not a failure.
      if (error instanceof HttpError && error.status === 404) {
        return {
          tier: "A" as const,
          url: href,
          retrievedAt: now(),
          rows: [],
          note:
            `No pageview data for "${input.article}" in that window. The title ` +
            "must match Wikipedia's exactly — check it with the wikipedia tool. " +
            "Data also runs about two days behind, so a window ending today " +
            "may legitimately be empty.",
        };
      }
      throw error;
    }

    const days = (body.items ?? []).map((item) => ({
      date: toIso(item.timestamp),
      views: item.views ?? 0,
    }));

    const counts = days.map((day) => day.views);
    const total = counts.reduce((sum, value) => sum + value, 0);
    const mean = counts.length ? Math.round(total / counts.length) : 0;
    const peak = days.reduce<{ date?: string; views: number }>(
      (best, day) => (day.views > best.views ? day : best),
      { views: -1 },
    );

    // Newest first, matching every other time series in this registry.
    const { rows, truncated } = capRows([...days].reverse());

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${days.length} days, ${total.toLocaleString()} views total, mean ` +
        `${mean.toLocaleString()}/day` +
        (peak.date ? `, peak ${peak.views.toLocaleString()} on ${peak.date}` : "") +
        ". THIS MEASURES ATTENTION, NOT EVENTS. A spike means people read the " +
        "article, which happens on rumour, anniversaries, a passing mention on " +
        "television, and for no discoverable reason at all. It is a lead worth " +
        "following with a Tier A source — never a substitute for one, and never " +
        "evidence that anything occurred. Counts exclude known bots but not all " +
        "automated traffic, and run roughly two days behind.",
    };
  },
});
