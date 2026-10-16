import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { ShapeError, getText, getTextCached, htmlToText, now, truncate } from "../http.ts";

/**
 * NOAA Climate Prediction Center — El Niño / La Niña (ENSO) state.
 *
 * Two kinds of answer, kept apart because one is measured and one is forecast:
 *   - `index` / `season_value`: the Oceanic Niño Index (ONI) and the Relative
 *     ONI (RONI), three-month sea-surface-temperature anomalies. Observations.
 *   - `discussion`: the monthly ENSO Diagnostic Discussion — alert status,
 *     synopsis and the stated strength probabilities. Those are forecasts.
 *
 * Classification uses CPC's published convention: values rounded to one
 * decimal, El Niño at +0.5 or above, La Niña at -0.5 or below. The ascii table
 * carries two decimals (1982 ASO = 1.45), and CPC's thresholds are applied to
 * the rounded figure (1.5), so rounding is not cosmetic — it decides the class.
 *
 * Verified 2026-10-15: ONI through JAS 2026 (+2.16), RONI through JAS 2026
 * (+1.69), discussion issued 8 October 2026 (El Niño Advisory).
 */

const ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt";
const RONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt";
const DISCUSSION_URL =
  "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/ensodisc.shtml";
const SEASONS = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"];
const TABLE_MAX_AGE_MS = 12 * 60 * 60 * 1000;

export type IndexKind = "oni" | "roni";
export type IndexRow = { season: string; year: number; anomaly: number; rounded: number };

/** One decimal, half away from zero, done on hundredths so 1.45 -> 1.5 exactly. */
export function roundCpc(value: number): number {
  const hundredths = Math.round(Math.abs(value) * 100);
  const tenths = Math.floor((hundredths + 5) / 10);
  return (Math.sign(value) * tenths) / 10;
}

/** Parse either CPC index table (ONI has SEAS YR TOTAL ANOM; RONI has SEAS YR ANOM). */
export function parseIndexTable(text: string, context: string): IndexRow[] {
  const rows: IndexRow[] = [];
  for (const line of text.split(/\r?\n/)) {
    const parts = line.trim().split(/\s+/);
    if (parts.length < 3 || !SEASONS.includes(parts[0])) continue;
    const year = Number(parts[1]);
    const anomaly = Number(parts[parts.length - 1]);
    if (!Number.isFinite(year) || !Number.isFinite(anomaly)) continue;
    rows.push({ season: parts[0], year, anomaly, rounded: roundCpc(anomaly) });
  }
  if (rows.length < 100) {
    throw new ShapeError(context, "a table of three-month index values", `${rows.length} parsable rows`);
  }
  return rows;
}

export async function loadIndex(kind: IndexKind, signal?: AbortSignal): Promise<IndexRow[]> {
  const url = kind === "roni" ? RONI_URL : ONI_URL;
  const text = await getTextCached(url, {
    signal, context: `CPC ${kind.toUpperCase()}`, timeoutMs: 20_000, maxAgeMs: TABLE_MAX_AGE_MS,
  });
  return parseIndexTable(text, `CPC ${kind.toUpperCase()}`);
}

/** year -> rounded value for one three-month window (e.g. ASO). */
export async function seasonValues(
  kind: IndexKind, season: string, signal?: AbortSignal,
): Promise<Map<number, number>> {
  const rows = await loadIndex(kind, signal);
  return new Map(rows.filter((row) => row.season === season).map((row) => [row.year, row.rounded]));
}

export function ensoClass(value: number): string {
  if (value >= 1.5) return "strong El Niño (>= +1.5)";
  if (value >= 0.5) return "El Niño (+0.5 to +1.4)";
  if (value <= -0.5) return "La Niña (<= -0.5)";
  return "neutral";
}

export type Discussion = {
  issued?: string;
  alertStatus?: string;
  synopsis?: string;
  probabilityStatements: string[];
  nextIssue?: string;
};

/** Pull the dated facts out of the discussion page (no model involved). */
export function parseDiscussion(html: string): Discussion {
  const flat = htmlToText(html);
  const issued = flat.match(/NCEP\/NWS\s+(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})/)?.[1];
  const alertStatus = flat.match(/ENSO Alert System Status:\s*(.+?)\s+Synopsis:/)?.[1]?.trim();
  const synopsis = flat.match(/Synopsis:\s*(.+?\.)\s/)?.[1]?.trim();
  const bodyStart = synopsis ? flat.indexOf(synopsis) + synopsis.length : 0;
  const bodyEnd = flat.indexOf("This discussion is a consolidated effort");
  const body = flat.slice(bodyStart, bodyEnd > bodyStart ? bodyEnd : undefined);
  const probabilityStatements = (body.match(/[^.]*\d+%[^.]*\./g) ?? [])
    .map((sentence) => sentence.trim())
    .slice(0, 6);
  const nextIssue = flat.match(/next ENSO diagnostics discussion is scheduled for ([^.]+)\./i)?.[1]?.trim();
  return { issued, alertStatus, synopsis, probabilityStatements, nextIssue };
}

const params = z.object({
  mode: z
    .enum(["index", "season_value", "discussion"])
    .describe(
      "index: three-month values (latest first, or a year range). season_value: one " +
        "value per year for one three-month window, with its ENSO class. discussion: " +
        "CPC's latest official ENSO discussion (status, synopsis, probabilities).",
    ),
  index: z.enum(["oni", "roni"]).optional().describe("Which index. Default oni (the traditional ONI)."),
  season: z
    .enum(SEASONS as [string, ...string[]])
    .optional()
    .describe("Three-month window for season_value, e.g. ASO (Aug-Oct). Default ASO."),
  from: z.number().int().optional().describe("First year (index/season_value)."),
  to: z.number().int().optional().describe("Last year (index/season_value)."),
  latest: z.number().int().min(1).max(24).optional().describe("index only: the most recent N values. Default 12."),
});

export const cpcEnso = defineAdapter({
  id: "cpc_enso",
  name: "CPC El Niño / La Niña (ENSO)",
  tier: "A",
  domain: "statistics",
  answers:
    "The El Niño / La Niña state from NOAA's Climate Prediction Center: the Oceanic " +
    "Niño Index (ONI) and Relative ONI (RONI) by three-month window back to 1950, " +
    "each year's ENSO class (El Niño, La Niña, neutral, strong El Niño), and CPC's " +
    "latest official ENSO discussion with its alert status and stated strength " +
    "probabilities. Use it for 'how strong is El Niño', 'will La Niña develop', or " +
    "to classify past years by ENSO state. It is NOT a hurricane count, rainfall or " +
    "temperature forecast for any place — ENSO is a Pacific index; its effects " +
    "elsewhere are tendencies, not outcomes.",
  keywords: [
    "enso", "nino", "nina", "oni", "roni", "elnino", "lanina", "cpc",
    "oscillation", "sst", "teleconnection",
  ],
  keywordsRequired: true,
  paramsSchema: params,
  paramsHelp:
    "mode is required. index: {mode:'index', index?:'oni'|'roni', latest?:12} or with " +
    "from/to. season_value: {mode:'season_value', season:'ASO', from:1991, to:2025}. " +
    "discussion: {mode:'discussion'}.",

  async run(input, signal) {
    if (input.mode === "discussion") {
      const html = await getText(DISCUSSION_URL, { signal, context: "CPC ENSO discussion", timeoutMs: 20_000 });
      const found = parseDiscussion(html);
      if (!found.issued && !found.synopsis) {
        throw new ShapeError("CPC ENSO discussion", "an issue date and a synopsis", truncate(html.replace(/\s+/g, " "), 200));
      }
      return {
        tier: "A" as const,
        url: DISCUSSION_URL,
        retrievedAt: now(),
        rows: [found],
        note:
          "The alert status and the measured index values in the discussion are " +
          "observations; every probability is a FORECAST of the ENSO state, not of " +
          "any weather outcome. Discussions are issued monthly (second Thursday); " +
          "check `issued` before treating this as current.",
      };
    }

    const kind = input.index ?? "oni";
    const rows = await loadIndex(kind, signal);
    const url = kind === "roni" ? RONI_URL : ONI_URL;
    const inRange = (year: number) =>
      (input.from === undefined || year >= input.from) && (input.to === undefined || year <= input.to);

    if (input.mode === "season_value") {
      const season = input.season ?? "ASO";
      const picked = rows.filter((row) => row.season === season && inRange(row.year));
      const shown = picked.slice(-25);
      return {
        tier: "A" as const,
        url,
        retrievedAt: now(),
        rows: shown.map((row) => ({
          year: row.year, season, value: row.rounded, raw: row.anomaly, class: ensoClass(row.rounded),
        })),
        truncated: picked.length > shown.length,
        note:
          `${kind.toUpperCase()} ${season} values, rounded to one decimal as CPC classifies ` +
          "them (raw = the two-decimal table value). ONI and RONI are different indices — " +
          "do not mix them when comparing years." +
          (picked.length > shown.length ? ` Showing the last 25 of ${picked.length} years; narrow from/to.` : ""),
      };
    }

    const selected = input.from !== undefined || input.to !== undefined
      ? rows.filter((row) => inRange(row.year)).slice(-25)
      : rows.slice(-(input.latest ?? 12));
    return {
      tier: "A" as const,
      url,
      retrievedAt: now(),
      rows: selected.map((row) => ({
        season: row.season, year: row.year, value: row.rounded, raw: row.anomaly, class: ensoClass(row.rounded),
      })),
      note:
        `${kind.toUpperCase()} three-month anomalies (°C), observations. The latest window ` +
        "can be revised slightly next month.",
    };
  },
});
