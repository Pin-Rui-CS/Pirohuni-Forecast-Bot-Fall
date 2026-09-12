import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { capRows, getJson, isObject, now } from "../http.ts";

/**
 * IMF PortWatch — daily transit counts through maritime chokepoints.
 *
 * `api-registry-crossref.md` §2b is a correction TO the companion guide, which
 * had said no stable documented REST endpoint exists and routed these questions
 * to a download page plus licensed AIS. The crossref was right: the ArcGIS
 * FeatureServer is public, unauthenticated and linked from PortWatch's own Data
 * & Methodology page.
 *
 * Verified 2026-09-06: 28 chokepoints, including every one the document names —
 * Bab el-Mandeb Strait, Strait of Hormuz, Suez Canal, Taiwan Strait.
 *
 * Tier A for the transit counts, which are IMF's published estimates from AIS.
 * The important caveat is temporal, not epistemic, and §2b states it: the
 * dataset refreshes on Tuesdays, so the last few days are simply absent. The
 * newest row on the day of writing was seven days old. A question about "this
 * week" is usually not yet answerable here, and saying so is the correct
 * answer rather than a failure.
 */

const CHOKEPOINTS = [
  "Suez Canal",
  "Panama Canal",
  "Bosporus Strait",
  "Bab el-Mandeb Strait",
  "Malacca Strait",
  "Strait of Hormuz",
  "Cape of Good Hope",
  "Gibraltar Strait",
  "Dover Strait",
  "Oresund Strait",
  "Taiwan Strait",
  "Korea Strait",
  "Tsugaru Strait",
  "Luzon Strait",
  "Lombok Strait",
  "Ombai Strait",
  "Bohai Strait",
  "Torres Strait",
  "Sunda Strait",
  "Makassar Strait",
  "Magellan Strait",
  "Yucatan Channel",
  "Windward Passage",
  "Mona Passage",
  "Balabac Strait",
  "Bering Strait",
  "Mindoro Strait",
  "Kerch Strait",
] as const;

const params = z.object({
  chokepoint: z
    .enum(CHOKEPOINTS)
    .describe("Which chokepoint. Must match one of the listed names exactly."),
  from: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Earliest date, YYYY-MM-DD."),
  to: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/)
    .describe("Latest date, YYYY-MM-DD. Recent days may not exist yet."),
});

type Feature = { attributes?: Record<string, unknown> };
type Response = { features?: Feature[]; error?: { message?: string } };

const SERVICE =
  "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/" +
  "Daily_Chokepoints_Data/FeatureServer/0/query";

export const portwatch = defineAdapter({
  id: "imf_portwatch",
  name: "IMF PortWatch",
  tier: "A",
  domain: "markets",
  answers:
    "Daily ship transits through the world's maritime chokepoints — Suez, " +
    "Hormuz, Bab el-Mandeb, Panama, Taiwan Strait and 23 others — split by " +
    "vessel type (container, tanker, dry bulk, general cargo, roro) with " +
    "capacity in deadweight tonnes. Reach for it for questions about shipping " +
    "disruption, blockades, canal traffic or trade rerouting. It reports " +
    "TRANSITS, not closures or policy decisions: a chokepoint with low traffic " +
    "is not necessarily a closed one. Data lags by up to a week.",
  keywords: [
    "shipping", "chokepoint", "strait", "canal", "suez", "hormuz", "panama",
    "transit", "transits", "vessel", "vessels", "tanker", "container", "cargo",
    "trade", "maritime", "blockade", "closed", "traffic", "route", "rerouting",
    "taiwan", "mandeb", "malacca", "bosporus", "disruption", "freight",
  ],
  paramsSchema: params,
  paramsHelp:
    "All three required. chokepoint must be an exact name from the enum. " +
    "Because the data refreshes weekly, ask for a window ending a week or more " +
    "ago if you need it to be populated.",

  async run(input, signal) {
    const url = new URL(SERVICE);
    // The name is enum-constrained, so it cannot carry a quote into the SQL.
    url.searchParams.set(
      "where",
      `portname='${input.chokepoint}' AND date>=DATE '${input.from}' ` +
        `AND date<=DATE '${input.to}'`,
    );
    url.searchParams.set(
      "outFields",
      "date,portname,n_total,n_container,n_tanker,n_dry_bulk,n_general_cargo," +
        "n_roro,n_cargo,capacity,capacity_container,capacity_tanker",
    );
    url.searchParams.set("orderByFields", "date DESC");
    url.searchParams.set("returnGeometry", "false");
    url.searchParams.set("resultRecordCount", "60");
    url.searchParams.set("f", "json");

    const href = url.toString();
    const body = await getJson<Response>(href, {
      signal,
      context: "IMF PortWatch",
      // ArcGIS reports failures as a 200 carrying an `error` object, so a
      // present `features` array is the only proof the query actually ran.
      expect: (value) => isObject(value) && Array.isArray(value.features),
      expected: "an ArcGIS response with a features array",
    });

    const daily = (body.features ?? []).map((feature) => feature.attributes ?? {});
    const matched = daily.length;
    const { rows, truncated } = capRows(daily);

    const totals = daily
      .map((day) => Number(day.n_total))
      .filter((value) => Number.isFinite(value));
    const mean =
      totals.length > 0
        ? (totals.reduce((sum, value) => sum + value, 0) / totals.length).toFixed(1)
        : null;

    return {
      tier: "A" as const,
      url: href,
      retrievedAt: now(),
      rows,
      truncated,
      note:
        `${matched} days returned for ${input.chokepoint}` +
        (mean ? `, mean ${mean} transits/day` : "") +
        ". n_* are vessel counts, capacity_* are deadweight tonnes. " +
        "PortWatch refreshes on TUESDAYS, so the most recent few days are " +
        "usually missing entirely — an empty result for a recent window means " +
        "the data has not been published yet, NOT that no ships transited. " +
        "These are observed transits and say nothing directly about whether a " +
        "route is officially open, closed or under threat.",
    };
  },
});
