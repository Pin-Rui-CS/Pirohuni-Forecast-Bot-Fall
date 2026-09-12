import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * WHO Disease Outbreak News.
 *
 * `api-registry-crossref.md` §1 adopts this over the RSS feed and calls it "the
 * correct route for the Bundibugyo Ebola case-count and province-spread
 * questions". That judgement is right. The URL it gives is not.
 *
 * MEASURED 2026-09-06: the documented `/api/news/outbreaks` is live, returns
 * HTTP 200 and valid OData, and its `value` array is permanently EMPTY —
 * with or without parameters. An adapter built on it would have reported "no
 * outbreaks" forever, in perfectly good faith, which is the §3 failure mode
 * arriving through a correct-looking endpoint rather than a broken one.
 *
 * The collection that actually holds the data is `/api/news/diseaseoutbreaknews`.
 * Queried on the same day, its three most recent entries were the Bundibugyo
 * Ebola DONs for the DRC — precisely the questions §1 wanted it for.
 *
 * Tier A. A Disease Outbreak News item is WHO's own formal publication, so its
 * existence and date are facts rather than reports of facts. The case counts
 * INSIDE it are WHO's figures as of that publication and are routinely revised
 * upward, which the note below says.
 */

const params = z.object({
  search: z
    .string()
    .optional()
    .describe(
      'Match against the title, e.g. "Ebola", "cholera", "avian influenza", ' +
        'or a country name. Omit for the most recent outbreak news.',
    ),
  since: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Only items published on or after this date, YYYY-MM-DD."),
});

type Item = {
  Title?: string;
  OverrideTitle?: string;
  PublicationDateAndTime?: string;
  UrlName?: string;
  Summary?: string;
  Overview?: string;
  Epidemiology?: string;
  DonId?: string;
};

type Response = { value?: Item[] };

const BASE = "https://www.who.int/api/news/diseaseoutbreaknews";

/** WHO's fields carry HTML. The model wants the prose, not the markup. */
function plain(html: string | undefined, limit: number): string | undefined {
  if (!html) return undefined;
  return truncate(html.replace(/<[^>]*>/g, " ").replace(/&nbsp;/g, " "), limit);
}

export const who = defineAdapter({
  id: "who_outbreaks",
  name: "WHO Disease Outbreak News",
  tier: "A",
  domain: "health",
  answers:
    "WHO's formal Disease Outbreak News: which outbreaks it has reported, when, " +
    "in which country, with case and death counts and its epidemiological " +
    "assessment. Resolution-grade for 'will WHO report an outbreak of X' and " +
    "for the date a DON was published. Reach for it for Ebola, cholera, mpox, " +
    "avian influenza, Marburg, polio and similar. Do NOT use it for routine " +
    "disease surveillance statistics, and do NOT use it for US-only questions " +
    "where the CDC is the named source.",
  keywords: [
    "outbreak", "outbreaks", "who", "epidemic", "pandemic", "disease", "ebola",
    "cholera", "mpox", "marburg", "polio", "influenza", "avian", "flu",
    "measles", "dengue", "zika", "virus", "infection", "cases", "deaths",
    "health", "global", "spread", "transmission", "who's", "emergency",
  ],
  paramsSchema: params,
  paramsHelp:
    "Both fields optional. With neither you get the most recent outbreak news. " +
    "search matches the title only, so use a disease or country name.",

  async run(input, signal) {
    const url = new URL(BASE);
    url.searchParams.set("$orderby", "PublicationDateAndTime desc");
    url.searchParams.set("$top", "20");

    /*
     * OData filters, assembled rather than interpolated blindly. `search` is
     * model-supplied text going into a query language, so the quote character
     * is escaped the way OData wants it — doubled — before it can close the
     * literal early.
     */
    const filters: string[] = [];
    if (input.search) {
      const safe = input.search.replace(/'/g, "''");
      filters.push(`contains(Title,'${safe}')`);
    }
    if (input.since) {
      filters.push(`PublicationDateAndTime ge ${input.since}T00:00:00Z`);
    }
    if (filters.length) url.searchParams.set("$filter", filters.join(" and "));

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "WHO Disease Outbreak News",
      // An empty `value` is a legitimate answer; a MISSING one means we are not
      // talking to the collection we think we are. That distinction is the
      // whole reason this adapter does not use the documented URL.
      expect: (value) => isObject(value) && Array.isArray(value.value),
      expected: "an OData object with a value array",
    });

    const { rows, truncated } = capRows(
      (body.value ?? []).map((item) => ({
        title: truncate(item.OverrideTitle || item.Title || "", 300),
        publishedAt: item.PublicationDateAndTime,
        don: item.DonId,
        summary: plain(item.Summary, 500),
        epidemiology: plain(item.Epidemiology, 900),
        url: item.UrlName
          ? `https://www.who.int/emergencies/disease-outbreak-news/item/${item.UrlName}`
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
        "A DON is WHO's formal publication: its existence and date are settled. " +
        "The case and death counts inside it are WHO's figures AS OF that " +
        "publication and are routinely revised upward in later DONs, so cite " +
        "the count with the DON that carried it rather than as a current total.",
    };
  },
});
