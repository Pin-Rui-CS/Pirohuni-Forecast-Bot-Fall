import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * Metaculus — the forecasting community's own aggregate.
 *
 * `api-registry-crossref.md` §1 corrects the endpoint specifically: use
 * `/api/posts/`, not the older `/api2/questions/`, which is the version the
 * original registry had. That correction is right, but the document is now
 * wrong about something larger.
 *
 * MEASURED 2026-09-06: the endpoint returns 403 with "The API is only available
 * to authenticated users. Please create an account and use your API token."
 * Metaculus closed public read access at some point after the crossref was
 * written. So this adapter is gated on `METACULUS_API_TOKEN` and is simply
 * absent from the registry until one is set — the same treatment
 * `lib/search.ts` gives an unconfigured search provider.
 *
 * Tier B, and the reason is worth stating plainly to the model: a community
 * forecast is evidence about what informed people believe, never evidence about
 * the world. Quoting a Metaculus number as though it settled a question is the
 * single most tempting error this whole registry exists to prevent.
 */

const params = z.object({
  search: z
    .string()
    .min(2)
    .optional()
    .describe("Search question titles, e.g. \"US recession 2027\"."),
  id: z
    .string()
    .regex(/^\d+$/)
    .optional()
    .describe("Fetch one post by its numeric Metaculus ID."),
  status: z
    .enum(["open", "closed", "resolved"])
    .optional()
    .describe("Filter by question status. Defaults to open."),
});

type Post = {
  id?: number;
  title?: string;
  status?: string;
  published_at?: string;
  scheduled_close_time?: string;
  scheduled_resolve_time?: string;
  nr_forecasters?: number;
  question?: {
    type?: string;
    resolution?: string | null;
    aggregations?: {
      recency_weighted?: {
        latest?: { centers?: number[]; means?: number[] } | null;
      };
    };
  };
};

type ListResponse = { count?: number; results?: Post[] };

const BASE = "https://www.metaculus.com/api/posts/";

/** The community's current central estimate, when the aggregate exposes one. */
function centralEstimate(post: Post): number | undefined {
  const latest = post.question?.aggregations?.recency_weighted?.latest;
  return latest?.centers?.[0] ?? latest?.means?.[0];
}

function summarise(post: Post): Record<string, unknown> {
  const estimate = centralEstimate(post);
  return {
    id: post.id,
    title: truncate(post.title ?? "", 300),
    status: post.status,
    type: post.question?.type,
    communityForecast:
      typeof estimate === "number"
        ? `${(estimate * 100).toFixed(1)}%`
        : undefined,
    forecasters: post.nr_forecasters,
    closesAt: post.scheduled_close_time,
    resolvesAt: post.scheduled_resolve_time,
    resolution: post.question?.resolution ?? undefined,
    url: post.id ? `https://www.metaculus.com/questions/${post.id}/` : undefined,
  };
}

export const metaculus = defineAdapter({
  id: "metaculus",
  name: "Metaculus",
  tier: "B",
  domain: "forecasting",
  answers:
    "Whether a forecasting community has already asked this question, what " +
    "their aggregate probability is, how many forecasters contributed, and " +
    "when it closes or resolves. Useful as a SANITY CHECK on your own estimate " +
    "and for finding how a question was operationalised. NEVER cite it as " +
    "evidence about the world: it measures belief, not fact. A resolved " +
    "question's resolution field is the exception — that records the outcome.",
  keywords: [
    "forecast", "forecasting", "probability", "prediction", "predict", "odds",
    "likelihood", "metaculus", "community", "estimate", "chance", "will",
    "resolve", "resolution", "market", "consensus", "base", "rate",
  ],
  paramsSchema: params,
  paramsHelp:
    "Give search or id. search matches question titles; status defaults to " +
    "open questions only.",

  available: () => Boolean(process.env.METACULUS_API_TOKEN?.trim()),

  async run(input, signal) {
    const single = Boolean(input.id);
    const url = new URL(single ? `${BASE}${input.id}/` : BASE);

    if (!single) {
      if (input.search) url.searchParams.set("search", input.search);
      url.searchParams.set("statuses", input.status ?? "open");
      url.searchParams.set("limit", "10");
      url.searchParams.set("order_by", "-hotness");
    }

    const token = process.env.METACULUS_API_TOKEN?.trim();
    const href = url.toString();
    const body = await getJson<ListResponse | Post>(href, {
      signal,
      context: "Metaculus",
      headers: token ? { Authorization: `Token ${token}` } : undefined,
      expect: (value) =>
        isObject(value) && (Array.isArray(value.results) || "id" in value),
      expected: single
        ? "a post object with an id"
        : "an object with a results array",
    });

    const posts = single
      ? [body as Post]
      : ((body as ListResponse).results ?? []);
    const { rows, truncated } = capRows(posts.map(summarise));

    return {
      tier: "B" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        "A community forecast is an aggregate of opinions, not a measurement. " +
        "Use it to check your own reasoning or to see how a question was " +
        "worded; do not present it as the answer. Only the resolution field on " +
        "an already-resolved question records an outcome.",
    };
  },
});
