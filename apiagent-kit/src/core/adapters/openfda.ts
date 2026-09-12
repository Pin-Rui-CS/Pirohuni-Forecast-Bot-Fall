import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, HttpError, isObject, now, truncate } from "../http.ts";

/**
 * openFDA — drug, device and food records from the FDA.
 *
 * Tier A for enforcement and recall questions: an enforcement report IS the
 * recall, filed by the agency.
 *
 * Two things about this endpoint drive the code below. It answers "nothing
 * matched" with an HTTP 404 and an error envelope rather than an empty list, so
 * a 404 is translated into zero rows instead of an error. And keyless use is
 * capped at 240 requests/minute and 1000/day per IP, which is generous enough
 * that no key is wired up for the testing phase.
 */

const params = z.object({
  dataset: z
    .enum([
      "drug/event",
      "drug/label",
      "drug/enforcement",
      "device/event",
      "device/recall",
      "device/enforcement",
      "food/enforcement",
    ])
    .describe(
      "Which FDA dataset. '*/enforcement' is recalls, 'drug/event' is adverse " +
        "event reports, 'drug/label' is approved labelling.",
    ),
  search: z
    .string()
    .min(2)
    .describe(
      'openFDA search syntax, e.g. \'product_description:"peanut"\' or ' +
        "'report_date:[20260101 TO 20261231]'. A bare word searches all fields.",
    ),
});

type Response = {
  meta?: { results?: { total?: number } };
  results?: unknown[];
};

/** Keep the interesting fields and drop the rest — these records are enormous
 *  and most of each one is administrative. */
function summarise(dataset: string, record: unknown): unknown {
  if (!isObject(record)) return record;

  const pick = (...keys: string[]): Record<string, unknown> =>
    Object.fromEntries(
      keys
        .filter((key) => record[key] !== undefined)
        .map((key) => [
          key,
          typeof record[key] === "string"
            ? truncate(record[key] as string, 400)
            : record[key],
        ]),
    );

  if (dataset.endsWith("enforcement") || dataset.endsWith("recall")) {
    return pick(
      "recall_number",
      "status",
      "classification",
      "recalling_firm",
      "product_description",
      "reason_for_recall",
      "recall_initiation_date",
      "report_date",
      "distribution_pattern",
      "voluntary_mandated",
    );
  }
  if (dataset === "drug/label") {
    return pick("effective_time", "indications_and_usage", "warnings", "openfda");
  }
  return pick(
    "safetyreportid",
    "receivedate",
    "serious",
    "patient",
    "primarysource",
  );
}

export const openFda = defineAdapter({
  id: "openfda",
  name: "openFDA",
  tier: "A",
  domain: "health",
  answers:
    "FDA recalls and enforcement actions (drug, device, food), adverse event " +
    "reports, and approved drug labelling. Resolution-grade for 'will there be " +
    "a Class I recall of X' and for counting recalls in a window. Do NOT use " +
    "it for drug APPROVAL decisions or advisory committee votes — those are " +
    "not in these datasets.",
  keywords: [
    "fda", "recall", "recalls", "enforcement", "adverse", "event", "drug",
    "device", "food", "contamination", "salmonella", "listeria", "safety",
    "label", "labelling", "warning", "class", "firm", "outbreak", "medical",
    "pharmaceutical", "injury", "defect",
  ],
  paramsSchema: params,
  paramsHelp:
    "dataset and search are both required. Date ranges use YYYYMMDD inside " +
    "square brackets, e.g. report_date:[20260801 TO 20260831].",

  async run(input, signal) {
    const url = new URL(`https://api.fda.gov/${input.dataset}.json`);
    url.searchParams.set("search", input.search);
    url.searchParams.set("limit", "20");
    const href = url.toString();

    let body: Response;
    try {
      body = await getJson<Response>(href, {
        signal,
        context: "openFDA",
        expect: (value) => isObject(value) && Array.isArray(value.results),
        expected: "an object with a results array",
      });
    } catch (error) {
      // "No matches found" arrives as a 404. That is an answer — zero — not a
      // failure, and reporting it as an error would let the model conclude the
      // API is broken when it has in fact told us something.
      if (error instanceof HttpError && error.status === 404) {
        return {
          tier: "A" as const,
          url: href,
          retrievedAt: now(),
          rows: [],
          note: "openFDA returned no matching records for this search.",
        };
      }
      throw error;
    }

    const { rows, truncated } = capRows(
      (body.results ?? []).map((record) => summarise(input.dataset, record)),
    );

    const total = body.meta?.results?.total;
    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        total !== undefined && total > rows.length
          ? `${total} records match; showing ${rows.length}.`
          : undefined,
    };
  },
});
