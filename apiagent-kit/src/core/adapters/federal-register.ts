import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * Federal Register — US rules, proposed rules, notices and executive orders.
 *
 * Tier A. This is the document of record: a rule is legally published here, so
 * an entry is the fact rather than a report of it.
 */

const params = z.object({
  term: z.string().min(2).describe("Full-text search over title and abstract."),
  agency: z
    .string()
    .optional()
    .describe('Agency slug, e.g. "environmental-protection-agency". Omit if unsure.'),
  type: z
    .enum(["RULE", "PRORULE", "NOTICE", "PRESDOCU"])
    .optional()
    .describe(
      "RULE = final rule, PRORULE = proposed, NOTICE = notice, " +
        "PRESDOCU = presidential document including executive orders.",
    ),
  since: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Earliest publication date, YYYY-MM-DD."),
  until: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Latest publication date, YYYY-MM-DD."),
});

type Response = {
  count?: number;
  results?: Array<{
    title?: string;
    document_number?: string;
    publication_date?: string;
    type?: string;
    abstract?: string;
    html_url?: string;
    agencies?: Array<{ name?: string }>;
  }>;
};

export const federalRegister = defineAdapter({
  id: "federal_register",
  name: "Federal Register",
  tier: "A",
  domain: "regulation",
  answers:
    "Whether a US federal rule, proposed rule, notice or executive order has " +
    "been published, and on what date. Resolution-grade for anything phrased " +
    "as 'will the agency issue/finalise/publish X' — publication here IS the " +
    "act. Do NOT use it for bills in Congress (that is a different process) or " +
    "for agency press releases.",
  keywords: [
    "rule", "rulemaking", "regulation", "regulatory", "federal", "register",
    "agency", "executive", "order", "notice", "proposed", "final", "epa",
    "fda", "ftc", "sec", "comment", "period", "docket", "cfr", "published",
    "issue", "promulgate", "repeal", "administration",
  ],
  paramsSchema: params,
  paramsHelp:
    "term is required. Narrow with type (RULE/PRORULE/NOTICE/PRESDOCU) and a " +
    "since/until date range when the question names a window.",

  async run(input, signal) {
    const url = new URL("https://www.federalregister.gov/api/v1/documents.json");
    url.searchParams.set("conditions[term]", input.term);
    url.searchParams.set("per_page", "20");
    url.searchParams.set("order", "newest");
    for (const field of [
      "title",
      "document_number",
      "publication_date",
      "type",
      "abstract",
      "html_url",
      "agencies",
    ]) {
      url.searchParams.append("fields[]", field);
    }
    if (input.agency) url.searchParams.set("conditions[agencies][]", input.agency);
    if (input.type) url.searchParams.set("conditions[type][]", input.type);
    if (input.since) {
      url.searchParams.set("conditions[publication_date][gte]", input.since);
    }
    if (input.until) {
      url.searchParams.set("conditions[publication_date][lte]", input.until);
    }

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "Federal Register",
      // `results` is absent when nothing matched, so only the envelope is
      // guaranteed. A `count` proves we reached the API and not a proxy page.
      expect: (value) => isObject(value) && "count" in value,
      expected: "an object with a count field",
    });

    const { rows, truncated } = capRows(
      (body.results ?? []).map((entry) => ({
        title: truncate(entry.title ?? "", 300),
        type: entry.type,
        publishedOn: entry.publication_date,
        documentNumber: entry.document_number,
        agencies: (entry.agencies ?? []).map((agency) => agency.name).filter(Boolean),
        abstract: entry.abstract ? truncate(entry.abstract, 500) : undefined,
        url: entry.html_url,
      })),
    );

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        body.count !== undefined && body.count > rows.length
          ? `${body.count} documents match; showing the ${rows.length} newest.`
          : undefined,
    };
  },
});
