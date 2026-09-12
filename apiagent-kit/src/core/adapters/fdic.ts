import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now } from "../http.ts";

/**
 * FDIC BankFind — failed US banks.
 *
 * `api-registry-crossref.md` §1 lists this as a straight omission from the
 * original registry and gives it as the canonical example of a DIRECT hit: "how
 * many U.S. banks will fail in August 2026" is answered by one filtered call,
 * not by inference from news coverage.
 *
 * Tier A. The FDIC is the receiver; its list is the fact.
 */

const params = z.object({
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Earliest failure date, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Latest failure date, YYYY-MM-DD."),
  state: z
    .string()
    .regex(/^[A-Za-z]{2}$/)
    .optional()
    .describe('Two-letter state code to narrow by, e.g. "CA".'),
});

/** FDIC wraps every row in its own `data` envelope. */
type Response = {
  meta?: { total?: number };
  data?: Array<{ data?: Record<string, unknown> }>;
};

export const fdic = defineAdapter({
  id: "fdic_failures",
  name: "FDIC BankFind (failures)",
  tier: "A",
  domain: "banking",
  answers:
    "US bank and thrift failures: which institutions failed, when, where, " +
    "their assets and deposits at failure, and the resolution type. " +
    "Resolution-grade for 'how many banks will fail between DATE and DATE' — " +
    "the count is the answer, not an estimate. Do NOT use it for bank " +
    "mergers, closures that are not failures, or credit union failures (a " +
    "different regulator).",
  keywords: [
    "bank", "banks", "failure", "failures", "failed", "fdic", "thrift",
    "insolvent", "collapse", "receivership", "deposits", "banking", "crisis",
    "institution", "closed", "regulator", "assets",
  ],
  paramsSchema: params,
  paramsHelp:
    "from and to are both required and inclusive. Use a whole month when the " +
    "question names one.",

  async run(input, signal) {
    const url = new URL("https://api.fdic.gov/banks/failures");
    url.searchParams.set(
      "filters",
      `FAILDATE:[${input.from} TO ${input.to}]` +
        (input.state ? ` AND STALP:${input.state.toUpperCase()}` : ""),
    );
    url.searchParams.set(
      "fields",
      "NAME,CERT,CITYST,STALP,FAILDATE,QBFASSET,QBFDEP,COST,RESTYPE,CHCLASS1",
    );
    url.searchParams.set("sort_by", "FAILDATE");
    url.searchParams.set("sort_order", "DESC");
    url.searchParams.set("limit", "25");
    url.searchParams.set("format", "json");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "FDIC BankFind",
      expect: (value) => isObject(value) && Array.isArray(value.data),
      expected: "an object with a data array",
    });

    const { rows, truncated } = capRows(
      (body.data ?? []).map((entry) => entry.data ?? {}),
    );

    const total = body.meta?.total;
    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${total ?? rows.length} failures in this range. ` +
        "QBFASSET and QBFDEP are assets and deposits at failure in thousands " +
        "of dollars. A zero count is a real answer: no banks failed.",
    };
  },
});
