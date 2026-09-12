import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { getJson, isObject, MAX_RESULT_CHARS, now, truncate } from "../http.ts";

/**
 * Wikipedia via MediaWiki `action=parse`.
 *
 * `api-registry-crossref.md` §5 makes a specific argument for this: the
 * companion guide writes off polling questions as having no API, but
 * Wikipedia's opinion-polling pages are maintained tables that "parse cleanly
 * from wikitext (not HTML)". Hence `prop=wikitext` rather than the rendered
 * page — a table is legible as wikitext markup and a mess as HTML.
 *
 * Tier B throughout. Wikipedia is a tertiary source: excellent for finding the
 * shape of an answer, never the thing that settles it.
 */

const params = z.object({
  mode: z
    .enum(["page", "search"])
    .optional()
    .describe(
      "search = find article titles matching a phrase. page = fetch one " +
        "article's wikitext. Defaults to page.",
    ),
  page: z
    .string()
    .min(2)
    .optional()
    .describe(
      'Exact article title, e.g. "Opinion polling for the next United Kingdom ' +
        'general election". Required for mode=page. A wrong title returns ' +
        "suggestions rather than an error.",
    ),
  query: z
    .string()
    .min(2)
    .optional()
    .describe('For mode=search, e.g. "2026 Atlantic hurricane season".'),
  section: z
    .string()
    .regex(/^\d+$/)
    .optional()
    .describe(
      "Section number to fetch alone. Use it for long articles — section 0 is " +
        "the lead. Omit for the whole page.",
    ),
});

type ParseResponse = {
  parse?: { title?: string; pageid?: number; wikitext?: string };
  error?: { code?: string; info?: string };
};

type SearchResponse = {
  query?: { search?: Array<{ title?: string; snippet?: string; wordcount?: number }> };
};

const API = "https://en.wikipedia.org/w/api.php";

/** Titles close to one that missed, so a wrong guess costs a note not a step. */
async function suggest(page: string, signal: AbortSignal): Promise<string[]> {
  const url = new URL(API);
  url.searchParams.set("action", "query");
  url.searchParams.set("list", "search");
  url.searchParams.set("srsearch", page);
  url.searchParams.set("srlimit", "5");
  url.searchParams.set("format", "json");
  url.searchParams.set("formatversion", "2");

  try {
    const body = await getJson<SearchResponse>(url.toString(), {
      signal,
      context: "Wikipedia search",
      expect: (value) => isObject(value),
      expected: "a MediaWiki response object",
    });
    return (body.query?.search ?? [])
      .map((hit) => hit.title)
      .filter((title): title is string => Boolean(title));
  } catch {
    // Suggestions are a courtesy. Failing to get them must not turn a missing
    // page into a failed tool call.
    return [];
  }
}

export const wikipedia = defineAdapter({
  id: "wikipedia",
  name: "Wikipedia (wikitext)",
  tier: "B",
  domain: "reference",
  answers:
    "The raw wikitext of an English Wikipedia article. Best for maintained " +
    "TABLES that no other API exposes — opinion polling averages, election " +
    "results, ongoing event trackers, lists of records. Reach for it when a " +
    "question needs a curated series that has no official feed. Do NOT cite it " +
    "as a resolution source, and do NOT use it for anything with a real API " +
    "elsewhere in this registry.",
  keywords: [
    "wikipedia", "poll", "polling", "polls", "average", "list", "table",
    "history", "background", "who", "what", "record", "results", "election",
    "approval", "ranking", "encyclopedia", "summary", "timeline", "overview",
  ],
  paramsSchema: params,
  paramsHelp:
    "mode=search with a query finds article titles; mode=page (the default) " +
    "with an exact page title returns its wikitext. If you do not know the " +
    "title, search first. A wrong title returns suggestions rather than an " +
    "error, so guessing once is cheap either way.",

  async run(input, signal) {
    /*
     * Search, exposed.
     *
     * The machinery already existed but ran only internally, to suggest titles
     * after a miss — which meant the adapter was usable only when the model
     * ALREADY knew the exact article name. Exposing it turns a retrieval tool
     * into one that can also explore.
     */
    if (input.mode === "search") {
      if (!input.query) throw new Error("query is required when mode is search.");

      const searchUrl = new URL(API);
      searchUrl.searchParams.set("action", "query");
      searchUrl.searchParams.set("list", "search");
      searchUrl.searchParams.set("srsearch", input.query);
      searchUrl.searchParams.set("srlimit", "10");
      searchUrl.searchParams.set("format", "json");
      searchUrl.searchParams.set("formatversion", "2");

      const found = await getJson<SearchResponse>(searchUrl.toString(), {
        signal,
        context: "Wikipedia search",
        expect: (value) => isObject(value),
        expected: "a MediaWiki response object",
      });

      const hits = found.query?.search ?? [];
      return {
        tier: "B" as const,
        url: searchUrl.toString(),
        retrievedAt: now(),
        rows: hits.map((hit) => ({
          title: hit.title,
          words: hit.wordcount,
          // MediaWiki marks matched terms with HTML in snippets.
          snippet: hit.snippet
            ? truncate(hit.snippet.replace(/<[^>]*>/g, ""), 250)
            : undefined,
        })),
        note:
          hits.length === 0
            ? "Nothing matched. Try fewer or more general words."
            : "Titles only. Call again with mode=page and one of these to read " +
              "the article. Wikipedia remains a tertiary source either way.",
      };
    }

    if (!input.page) throw new Error("page is required when mode is page.");

    const url = new URL(API);
    url.searchParams.set("action", "parse");
    url.searchParams.set("page", input.page);
    url.searchParams.set("prop", "wikitext");
    url.searchParams.set("format", "json");
    url.searchParams.set("formatversion", "2");
    if (input.section) url.searchParams.set("section", input.section);

    const href = url.toString();
    const body = await getJson<ParseResponse>(href, {
      signal,
      context: "Wikipedia",
      // MediaWiki reports a missing page as a 200 with an `error` object, so
      // both shapes are valid responses and only neither is a real failure.
      expect: (value) =>
        isObject(value) && (isObject(value.parse) || isObject(value.error)),
      expected: "a MediaWiki response with a parse or error section",
    });

    if (body.error || !body.parse?.wikitext) {
      const suggestions = await suggest(input.page, signal);
      return {
        tier: "B" as const,
        url: href,
        retrievedAt: now(),
        rows: [],
        note:
          `No article titled "${input.page}". ` +
          (suggestions.length
            ? `Closest titles: ${suggestions.join("; ")}.`
            : "No close titles found either."),
      };
    }

    const wikitext = body.parse.wikitext;
    const truncated = wikitext.length > MAX_RESULT_CHARS;

    return {
      tier: "B" as const,
      url: `https://en.wikipedia.org/wiki/${encodeURIComponent(input.page)}`,
      retrievedAt: now(),
      rows: [
        {
          title: body.parse.title,
          pageId: body.parse.pageid,
          wikitext: truncate(wikitext, MAX_RESULT_CHARS),
        },
      ],
      truncated,
      note:
        "Wikipedia is a tertiary source — good for finding a number, never for " +
        "settling one. Anyone can edit it and this snapshot has no revision " +
        "guarantee." +
        (truncated
          ? ` Article is ${wikitext.length} chars; truncated. Use the section ` +
            "parameter to fetch one part in full."
          : ""),
    };
  },
});
