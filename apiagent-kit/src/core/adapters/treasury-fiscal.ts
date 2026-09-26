import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { MAX_ROWS, capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * U.S. Treasury Fiscal Data — the resolution source for federal debt,
 * deficits, receipts and outlays, Treasury interest rates and cash balances.
 *
 * NOT FROM UPSTREAM. Added in the forecasting bot (2026-09-26) after question
 * 45412 ("US national debt on Dec 31, 2026") resolved on Debt to the Penny, a
 * JavaScript app page that the bot's scraper could not read, while the full
 * daily series sat behind this documented, keyless API. FRED only mirrors the
 * quarterly total with a lag.
 *
 * Every dataset page (fiscaldata.treasury.gov/datasets/<slug>/) is described in
 * the site's own metadata catalogue, including the API endpoint(s) behind it,
 * so `catalog` resolves any of the ~56 datasets without a hand-kept table.
 *
 * `limit` exists for programmatic callers that want a whole series (the bot's
 * measured-series path). Agent callers keep the MAX_ROWS default.
 */

const BASE = "https://api.fiscaldata.treasury.gov";
const API_PREFIX = "/services/api/fiscal_service/";
const CATALOG_URL = `${BASE}/services/dtg/metadata/`;
const TIMEOUT_MS = 30_000;
/** The API's own maximum page size. */
const PAGE_SIZE = 10_000;
const MAX_LIMIT = 50_000;

const params = z.object({
  mode: z
    .enum(["catalog", "observations"])
    .describe(
      "catalog = find a dataset and the API endpoint(s) behind it. " +
        "observations = the data rows from one endpoint.",
    ),
  dataset: z
    .string()
    .optional()
    .describe(
      'For catalog: a dataset slug from a fiscaldata.treasury.gov/datasets/<slug>/ ' +
        'URL (e.g. "debt-to-the-penny"), or words to search dataset titles.',
    ),
  endpoint: z
    .string()
    .regex(/^(\/?services\/api\/fiscal_service\/)?v[12]\/accounting\/[a-z0-9_/]+$/)
    .optional()
    .describe(
      'For observations: the endpoint path from catalog, e.g. ' +
        '"v2/accounting/od/debt_to_penny".',
    ),
  fields: z
    .string()
    .regex(/^[a-z0-9_,]+$/)
    .optional()
    .describe('Comma-separated columns, e.g. "record_date,tot_pub_debt_out_amt".'),
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Earliest record_date, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Latest record_date, YYYY-MM-DD."),
  limit: z.number().int().min(1).max(MAX_LIMIT).optional(),
});

type Params = z.infer<typeof params>;

type CatalogField = {
  column_name?: string;
  pretty_name?: string;
  data_type?: string;
  definition?: string;
};
type CatalogApi = {
  endpoint_txt?: string;
  table_name?: string;
  row_definition?: string;
  update_frequency?: string;
  earliest_date?: string;
  latest_date?: string;
  row_count?: number;
  fields?: CatalogField[];
};
type CatalogDataset = {
  dataset_path?: string;
  title?: string;
  update_frequency?: string;
  apis?: CatalogApi[];
};

type DataResponse = {
  data?: Array<Record<string, string | null>>;
  meta?: { "total-count"?: number; "total-pages"?: number; dataTypes?: Record<string, string> };
};

const NUMERIC_TYPES = /^(CURRENCY|NUMBER|PERCENTAGE|INTEGER|RATE)/;

function numericFields(api: CatalogApi): CatalogField[] {
  return (api.fields ?? []).filter(
    (f) => NUMERIC_TYPES.test(f.data_type ?? "") && !/_nbr$/.test(f.column_name ?? ""),
  );
}

async function runCatalog(input: Params, signal: AbortSignal) {
  const wanted = (input.dataset ?? "").trim().toLowerCase().replace(/^\/|\/$/g, "");
  if (!wanted) throw new Error("dataset is required when mode is catalog.");

  const catalog = await getJson<CatalogDataset[]>(CATALOG_URL, {
    signal,
    timeoutMs: TIMEOUT_MS,
    context: "Treasury Fiscal Data catalog",
    expect: (value) => Array.isArray(value) && value.length > 0,
    expected: "a non-empty array of datasets",
  });

  const bySlug = catalog.filter((d) => d.dataset_path === wanted);
  const words = wanted.split(/[\s-]+/).filter(Boolean);
  const matches = bySlug.length
    ? bySlug
    : catalog.filter((d) => {
        const title = (d.title ?? "").toLowerCase();
        return words.every((w) => title.includes(w));
      });

  const all = matches.flatMap((dataset) =>
    (dataset.apis ?? []).map((api) => ({
      dataset: dataset.dataset_path,
      title: dataset.title,
      table: api.table_name,
      endpoint: (api.endpoint_txt ?? "").replace(API_PREFIX, "").replace(/^\//, ""),
      rowDefinition: truncate(api.row_definition ?? "", 200),
      updateFrequency: api.update_frequency ?? dataset.update_frequency,
      covers: `${api.earliest_date} to ${api.latest_date}`,
      rowCount: api.row_count,
      // Line-number columns are bookkeeping, not measures.
      numericFields: numericFields(api).map((f) => f.column_name),
      labels: Object.fromEntries(
        numericFields(api).map((f) => [f.column_name, f.pretty_name ?? f.column_name]),
      ),
      dateField: (api.fields ?? []).some((f) => f.column_name === "record_date")
        ? "record_date"
        : undefined,
    })),
  );
  const { rows, truncated } = capRows(all);

  return {
    tier: "A" as const,
    url: CATALOG_URL,
    retrievedAt: now(),
    rows,
    truncated,
    note: rows.length
      ? "Call again with mode=observations and one of these endpoints."
      : `No dataset matched "${wanted}".`,
  };
}

function toRow(
  raw: Record<string, string | null>,
  types: Record<string, string>,
): Record<string, string | number | null> {
  const row: Record<string, string | number | null> = {};
  for (const [key, value] of Object.entries(raw)) {
    // The API writes every value as a string and a gap as "null".
    if (value === null || value === "null" || value === "") {
      row[key] = null;
    } else if (NUMERIC_TYPES.test(types[key] ?? "")) {
      const n = Number(value);
      row[key] = Number.isFinite(n) ? n : null;
    } else {
      row[key] = value;
    }
  }
  return row;
}

async function runObservations(input: Params, signal: AbortSignal) {
  if (!input.endpoint) throw new Error("endpoint is required when mode is observations.");
  const path = input.endpoint.replace(/^\/?services\/api\/fiscal_service\//, "");
  const want = input.limit ?? MAX_ROWS;

  const filters: string[] = [];
  if (input.from) filters.push(`record_date:gte:${input.from}`);
  if (input.to) filters.push(`record_date:lte:${input.to}`);

  const collected: Array<Record<string, string | number | null>> = [];
  let total = 0;
  let firstUrl = "";
  for (let page = 1; collected.length < want; page++) {
    const url = new URL(`${BASE}${API_PREFIX}${path}`);
    if (input.fields) url.searchParams.set("fields", input.fields);
    if (filters.length) url.searchParams.set("filter", filters.join(","));
    url.searchParams.set("sort", "-record_date");
    url.searchParams.set("page[size]", String(Math.min(PAGE_SIZE, want)));
    url.searchParams.set("page[number]", String(page));
    if (!firstUrl) firstUrl = url.toString();

    const body = await getJson<DataResponse>(url.toString(), {
      signal,
      timeoutMs: TIMEOUT_MS,
      context: "Treasury Fiscal Data",
      expect: (value) => isObject(value) && Array.isArray(value.data),
      expected: "an object with a data array",
    });
    const types = body.meta?.dataTypes ?? {};
    total = body.meta?.["total-count"] ?? total;
    for (const raw of body.data ?? []) collected.push(toRow(raw, types));
    if (page >= (body.meta?.["total-pages"] ?? 1) || !(body.data ?? []).length) break;
  }

  const { rows, truncated } = capRows(collected, want);

  return {
    tier: "A" as const,
    url: firstUrl,
    retrievedAt: now(),
    rows,
    truncated: truncated || total > rows.length,
    note:
      `${rows.length} of ${total} rows, newest record_date first. Amounts are in ` +
      "the units the column name states (_amt = dollars, _mil_amt = millions). " +
      "record_date is the date the figure is AS OF; Treasury publishes it on " +
      "the following business day.",
  };
}

export const treasuryFiscal = defineAdapter({
  id: "treasury_fiscal",
  name: "U.S. Treasury Fiscal Data",
  tier: "A",
  domain: "statistics",
  answers:
    "Official U.S. Treasury figures: total public debt outstanding by day " +
    "(Debt to the Penny), debt held by the public and intragovernmental, " +
    "monthly receipts, outlays and the budget deficit (Monthly Treasury " +
    "Statement), the Treasury's daily cash balance (Daily Treasury Statement), " +
    "average interest rates on Treasury securities, auctions and exchange rates. " +
    "Use mode=catalog to find the endpoint, then mode=observations. Do NOT use it " +
    "for market yields or economic statistics — that is FRED.",
  keywords: [
    "treasury", "debt", "national", "federal", "public", "penny", "deficit",
    "budget", "receipts", "outlays", "spending", "revenue", "cash", "balance",
    "fiscal", "interest", "securities", "auction", "auctions", "bills", "bonds",
    "notes", "borrowing", "intragovernmental", "mts", "dts", "government",
  ],
  paramsSchema: params,
  paramsHelp:
    'mode is required. catalog needs dataset (a slug like "debt-to-the-penny" ' +
    "or title words). observations needs endpoint from catalog; from/to filter " +
    "on record_date.",

  async run(input, signal) {
    if (input.mode === "catalog") return runCatalog(input, signal);
    return runObservations(input, signal);
  },
});
