import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now, truncate } from "../http.ts";

/**
 * CISA Known Exploited Vulnerabilities catalogue.
 *
 * Tier A. Addition to the KEV list is an administrative act by CISA, so
 * presence and `dateAdded` are facts rather than assessments.
 *
 * Unlike every other adapter here this is one whole file — roughly 1,300
 * entries, a couple of megabytes — with no server-side query. Filtering
 * therefore happens locally, and the file is cached per instance so that a turn
 * asking two questions about it does not download it twice.
 */

const params = z.object({
  query: z
    .string()
    .optional()
    .describe(
      "Case-insensitive match against CVE ID, vendor, product, or the " +
        "vulnerability name and description. Omit to get the newest additions.",
    ),
  addedSince: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .optional()
    .describe("Only entries added to the catalogue on or after this date."),
  ransomwareOnly: z
    .boolean()
    .optional()
    .describe("Restrict to vulnerabilities with known ransomware campaign use."),
});

const SOURCE =
  "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json";

type Vulnerability = {
  cveID?: string;
  vendorProject?: string;
  product?: string;
  vulnerabilityName?: string;
  dateAdded?: string;
  shortDescription?: string;
  requiredAction?: string;
  dueDate?: string;
  knownRansomwareCampaignUse?: string;
};

type Catalogue = {
  catalogVersion?: string;
  dateReleased?: string;
  count?: number;
  vulnerabilities?: Vulnerability[];
};

/**
 * Per-instance cache. Serverless means per-instance and gone on a cold start,
 * which is the right trade for a file that changes at most daily — the same
 * reasoning `lib/search.ts` applies to Brave's remembered quota.
 */
let cached: { at: number; catalogue: Catalogue } | null = null;
const CACHE_MS = 60 * 60 * 1000;

async function load(signal: AbortSignal): Promise<Catalogue> {
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.catalogue;

  const catalogue = await getJson<Catalogue>(SOURCE, {
    signal,
    context: "CISA KEV",
    expect: (value) => isObject(value) && Array.isArray(value.vulnerabilities),
    expected: "an object with a vulnerabilities array",
  });

  cached = { at: Date.now(), catalogue };
  return catalogue;
}

export const cisaKev = defineAdapter({
  id: "cisa_kev",
  name: "CISA KEV catalogue",
  tier: "A",
  domain: "security",
  answers:
    "Whether a specific CVE is on CISA's Known Exploited Vulnerabilities list, " +
    "when it was added, its remediation due date, and whether it is linked to " +
    "ransomware. Resolution-grade for 'will CVE-X be added to KEV' and for " +
    "counting additions in a window. Do NOT use it for CVE severity scores or " +
    "for vulnerabilities that are merely disclosed — KEV means actively " +
    "exploited in the wild.",
  keywords: [
    "cve", "vulnerability", "vulnerabilities", "exploit", "exploited", "kev",
    "cisa", "security", "patch", "zero-day", "ransomware", "breach", "advisory",
    "cybersecurity", "attack", "software", "flaw", "remediation",
  ],
  paramsSchema: params,
  paramsHelp:
    "Every field is optional. With no query you get the most recently added " +
    "entries. query matches CVE IDs, vendors and products.",

  async run(input, signal) {
    const catalogue = await load(signal);
    const needle = input.query?.toLowerCase().trim();

    let list = catalogue.vulnerabilities ?? [];

    if (needle) {
      list = list.filter((entry) =>
        [
          entry.cveID,
          entry.vendorProject,
          entry.product,
          entry.vulnerabilityName,
          entry.shortDescription,
        ]
          .filter((field): field is string => typeof field === "string")
          .some((field) => field.toLowerCase().includes(needle)),
      );
    }
    if (input.addedSince) {
      list = list.filter((entry) => (entry.dateAdded ?? "") >= input.addedSince!);
    }
    if (input.ransomwareOnly) {
      list = list.filter(
        (entry) => entry.knownRansomwareCampaignUse?.toLowerCase() === "known",
      );
    }

    // Newest first — the shape of nearly every question asked of this list.
    list = [...list].sort((a, b) => (b.dateAdded ?? "").localeCompare(a.dateAdded ?? ""));

    const matched = list.length;
    const { rows, truncated } = capRows(
      list.map((entry) => ({
        cve: entry.cveID,
        vendor: entry.vendorProject,
        product: entry.product,
        name: entry.vulnerabilityName,
        dateAdded: entry.dateAdded,
        dueDate: entry.dueDate,
        ransomware: entry.knownRansomwareCampaignUse,
        description: entry.shortDescription
          ? truncate(entry.shortDescription, 400)
          : undefined,
      })),
    );

    return {
      tier: "A" as const,
      url: SOURCE,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `Catalogue version ${catalogue.catalogVersion ?? "unknown"}, released ` +
        `${catalogue.dateReleased ?? "unknown"}, holding ${catalogue.count ?? "?"} ` +
        `entries. ${matched} matched this filter. Filtering is done locally on ` +
        `the full published file, so the count is exact rather than paginated.`,
    };
  },
});
