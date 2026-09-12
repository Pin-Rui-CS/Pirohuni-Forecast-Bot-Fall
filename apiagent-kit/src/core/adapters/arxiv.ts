import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getText, now, truncate, ShapeError } from "../http.ts";

/**
 * arXiv — preprints in physics, maths, CS, biology, economics.
 *
 * Tier A for existence and date: a preprint's arXiv posting is the publication
 * event. Not resolution-grade for a claim's TRUTH — a preprint is unrefereed,
 * and the description below says so.
 *
 * This is the only adapter returning XML. arXiv's Atom is small and regular, so
 * it is parsed with a handful of regexes rather than by pulling a DOM library
 * into the smoke test's import graph. If arXiv ever returns something that does
 * not parse, the shape check below throws rather than silently yielding zero
 * results — the §3 rule applied to a format that is not JSON.
 */

const params = z.object({
  query: z
    .string()
    .min(2)
    .describe(
      'Search terms. Field prefixes work: ti: (title), au: (author), ' +
        'abs: (abstract), cat: (category, e.g. cat:cs.LG). Bare words search all.',
    ),
  sortBy: z
    .enum(["relevance", "submittedDate", "lastUpdatedDate"])
    .optional()
    .describe("Defaults to submittedDate, newest first."),
});

const ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: String.fromCharCode(34),
  apos: "'",
};

function decode(text: string): string {
  return text
    .replace(/&#(\d+);/g, (_, code: string) => String.fromCharCode(Number(code)))
    .replace(/&(amp|lt|gt|quot|apos);/g, (_, name: string) => ENTITIES[name] ?? _);
}

/** First `<tag>…</tag>` inside a fragment. arXiv never nests these. */
function tag(fragment: string, name: string): string | undefined {
  const match = new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`).exec(
    fragment,
  );
  return match ? decode(match[1]).replace(/\s+/g, " ").trim() : undefined;
}

function allTags(fragment: string, name: string): string[] {
  const found: string[] = [];
  const pattern = new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "g");
  let match = pattern.exec(fragment);
  while (match) {
    found.push(decode(match[1]).replace(/\s+/g, " ").trim());
    match = pattern.exec(fragment);
  }
  return found;
}

export const arxiv = defineAdapter({
  id: "arxiv",
  name: "arXiv",
  tier: "A",
  domain: "science",
  answers:
    "Whether a preprint exists on a topic, who wrote it, when it was posted, " +
    "and its abstract. Resolution-grade for 'will a paper on X appear by " +
    "DATE' and for counting submissions in a category. Do NOT treat a preprint " +
    "as an established result — it is unrefereed — and do NOT use it for " +
    "journal publication dates, which are a separate later event.",
  keywords: [
    "paper", "papers", "preprint", "arxiv", "research", "publication",
    "author", "abstract", "physics", "mathematics", "machine", "learning",
    "computer", "science", "biology", "economics", "study", "submitted",
    "published", "academic", "citation", "model", "algorithm",
  ],
  paramsSchema: params,
  paramsHelp:
    'query is required, e.g. \'cat:cs.LG AND abs:"mixture of experts"\'. ' +
    "sortBy defaults to newest first.",

  async run(input, signal) {
    const url = new URL("https://export.arxiv.org/api/query");
    url.searchParams.set("search_query", input.query);
    url.searchParams.set("start", "0");
    url.searchParams.set("max_results", "20");
    url.searchParams.set("sortBy", input.sortBy ?? "submittedDate");
    url.searchParams.set("sortOrder", "descending");

    const href = url.toString();
    const xml = await getText(href, { signal, context: "arXiv" });

    // A valid response is always an Atom feed, empty result set or not. Anything
    // else — a rate-limit page, an outage notice — must not read as "no papers".
    if (!xml.includes("<feed")) {
      throw new ShapeError("arXiv", "an Atom feed", truncate(xml, 200));
    }

    const entries = xml.split("<entry>").slice(1);
    const { rows, truncated } = capRows(
      entries.map((entry) => {
        const id = tag(entry, "id");
        return {
          title: truncate(tag(entry, "title") ?? "", 300),
          authors: allTags(entry, "name").slice(0, 8),
          published: tag(entry, "published"),
          updated: tag(entry, "updated"),
          summary: truncate(tag(entry, "summary") ?? "", 600),
          url: id,
        };
      }),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        "Preprints are not peer reviewed. The posting date is a fact; the " +
        "paper's claims are not established by appearing here.",
    };
  },
});
