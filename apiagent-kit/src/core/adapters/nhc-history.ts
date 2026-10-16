import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { ShapeError, getText, getTextCached, now } from "../http.ts";
import { ensoClass, loadIndex, seasonValues, type IndexKind } from "./cpc-enso.ts";

/**
 * National Hurricane Center — tropical cyclone history (HURDAT2) and the
 * current season's running tally.
 *
 * HURDAT2 is NHC's post-season best-track file: every storm since 1851
 * (Atlantic) or 1949 (NE Pacific), six-hourly. Answers here are COMPUTED from
 * it, never pasted, because the file is ~7 MB and the questions it answers —
 * "how many storms form after mid-October in El Niño years" — need counting:
 *
 *   - a storm is NAMED when a track point has status TS, SS or HU with wind
 *     >= 34 kt; its qualifying moment is the first such point (UTC);
 *   - hurricane = peak >= 64 kt as HU; major = peak >= 96 kt;
 *   - ACE = sum(wind^2) / 1e4 over the 00/06/12/18 UTC points while TS/SS/HU
 *     at >= 34 kt;
 *   - a storm belongs to the season in its identifier (AL012025 -> 2025).
 *
 * The current season is not in HURDAT2 until the next release, so
 * `season_to_date` reads NHC's season summary page instead.
 *
 * Verified 2026-10-15: newest Atlantic file hurdat2-1851-2025-092326.txt
 * (7,071,568 bytes, under the 16 MiB maxBytes set here); 1991-2025 mean of
 * named storms first qualifying after 14 Oct 05:05 UTC = 2.14, after 21 Oct
 * = 1.74, matching an independent hand count. 2026 summary page: 9 named storms
 * as of 03 UTC 15 October 2026.
 */

const INDEX_URL = "https://www.nhc.noaa.gov/data/hurdat/";
const TCR_URL = "https://www.nhc.noaa.gov/data/tcr/index.php";
const FILE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // filenames are versioned
const INDEX_MAX_AGE_MS = 12 * 60 * 60 * 1000;

export type Basin = "atlantic" | "pacific";
export type Storm = {
  id: string;
  name: string;
  season: number;
  qualified?: string; // YYYYMMDDHHMM UTC, first TS/SS/HU point >= 34 kt
  peakWind: number;
  hurricane: boolean;
  major: boolean;
  ace: number;
};

/** Newest file per basin from the directory listing, by its listed date. */
export function newestFile(indexHtml: string, basin: Basin): string {
  const prefix = basin === "atlantic" ? "hurdat2-1851-" : "hurdat2-nepac-";
  const pattern = /href="(hurdat2-[^"]+\.txt)"[\s\S]*?<td[^>]*>\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})/g;
  let best: { file: string; at: string } | undefined;
  for (const match of indexHtml.matchAll(pattern)) {
    const [, file, at] = match;
    if (!file.startsWith(prefix)) continue;
    if (!best || at > best.at) best = { file, at };
  }
  if (!best) throw new ShapeError("NHC HURDAT2 index", `a ${prefix}*.txt file`, "no matching file listed");
  return best.file;
}

export function parseHurdat2(text: string): Storm[] {
  const storms: Storm[] = [];
  let current: Storm | undefined;
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const parts = line.split(",").map((part) => part.trim());
    if (/^[A-Z]{2}\d{6}$/.test(parts[0])) {
      current = {
        id: parts[0], name: parts[1], season: Number(parts[0].slice(4)),
        peakWind: 0, hurricane: false, major: false, ace: 0,
      };
      storms.push(current);
      continue;
    }
    if (!current || parts.length < 7) continue;
    const [date, time, , status] = parts;
    const wind = Number(parts[6]);
    if (!Number.isFinite(wind)) continue;
    const tropical = status === "TS" || status === "SS" || status === "HU";
    if (tropical && wind >= 34) {
      current.qualified ??= `${date}${time.padStart(4, "0")}`;
      if (["0000", "0600", "1200", "1800"].includes(time.padStart(4, "0"))) current.ace += (wind * wind) / 1e4;
    }
    if (tropical) current.peakWind = Math.max(current.peakWind, wind);
    if (status === "HU" && wind >= 64) current.hurricane = true;
    if (status === "HU" && wind >= 96) current.major = true;
  }
  if (storms.length < 100) throw new ShapeError("NHC HURDAT2", "a best-track file with storm headers", `${storms.length} storms`);
  return storms;
}

async function loadStorms(basin: Basin, signal: AbortSignal): Promise<{ storms: Storm[]; url: string }> {
  const index = await getTextCached(INDEX_URL, {
    signal, context: "NHC HURDAT2 index", timeoutMs: 20_000, maxAgeMs: INDEX_MAX_AGE_MS,
  });
  const url = INDEX_URL + newestFile(index, basin);
  const text = await getTextCached(url, {
    signal, context: "NHC HURDAT2", timeoutMs: 45_000, maxAgeMs: FILE_MAX_AGE_MS,
    maxBytes: 16 * 1024 * 1024,
  });
  return { storms: parseHurdat2(text), url };
}

/** "10-14" or "10-14T05:05" -> "10140505"-style key compared against MMDDHHMM. */
export function monthDayKey(value: string, endOfDay: boolean): string {
  const match = value.match(/^(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/);
  if (!match) throw new Error(`expected MM-DD or MM-DDTHH:MM, got ${value}`);
  const [, mm, dd, hh, mi] = match;
  return `${mm}${dd}${hh ?? (endOfDay ? "23" : "00")}${mi ?? (endOfDay ? "59" : "00")}`;
}

type SeasonStat = { season: number; value: number };

function summarise(values: SeasonStat[]) {
  const sorted = values.map((v) => v.value).sort((a, b) => a - b);
  const n = sorted.length;
  const mean = n ? sorted.reduce((a, b) => a + b, 0) / n : 0;
  const median = n ? (n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2) : 0;
  const distribution: Record<string, number> = {};
  for (const v of sorted) distribution[v] = (distribution[v] ?? 0) + 1;
  return {
    seasons: n, mean: Number(mean.toFixed(2)), median, min: sorted[0], max: sorted[n - 1], distribution,
  };
}

const compact = (values: SeasonStat[]) => values.map((v) => `${v.season}:${Number(v.value.toFixed(1))}`).join(" ");

async function groupByEnso(
  values: SeasonStat[], kind: IndexKind, window: string, signal: AbortSignal,
) {
  const enso = await seasonValues(kind, window, signal);
  const buckets: Array<[string, (x: number) => boolean]> = [
    [`${kind.toUpperCase()} ${window} >= +1.5 (strong El Niño)`, (x) => x >= 1.5],
    [`${kind.toUpperCase()} ${window} >= +0.5 (any El Niño, includes strong)`, (x) => x >= 0.5],
    [`${kind.toUpperCase()} ${window} between -0.4 and +0.4 (neutral)`, (x) => x > -0.5 && x < 0.5],
    [`${kind.toUpperCase()} ${window} <= -0.5 (La Niña)`, (x) => x <= -0.5],
  ];
  return buckets.map(([group, test]) => {
    const members = values.filter((v) => enso.has(v.season) && test(enso.get(v.season)!));
    const s = summarise(members);
    return { group, seasons: s.seasons, mean: s.mean, median: s.median, perSeason: compact(members) };
  }).concat(values.some((v) => !enso.has(v.season))
    ? [{ group: "no ENSO value for these seasons", seasons: values.filter((v) => !enso.has(v.season)).length, mean: 0, median: 0, perSeason: "" }]
    : []);
}

/** The latest three-month ONI and RONI, so a reader can pick the matching class. */
async function currentEnso(signal: AbortSignal) {
  const [oni, roni] = await Promise.all([loadIndex("oni", signal), loadIndex("roni", signal)]);
  const last = (rows: Awaited<ReturnType<typeof loadIndex>>) => rows[rows.length - 1];
  const o = last(oni);
  const r = last(roni);
  return {
    group: "current ENSO state (latest three-month values)",
    oni: `${o.season} ${o.year}: ${o.rounded} (${ensoClass(o.rounded)})`,
    roni: `${r.season} ${r.year}: ${r.rounded} (${ensoClass(r.rounded)})`,
    hint: "For CPC's forecast of where it is heading, call cpc_enso with mode 'discussion'.",
  };
}

export type SeasonToDate = {
  asOf?: string;
  storms: Array<{ number: number; name: string; type: string; dates: string; maxWindKt: number; ace: number }>;
  namedStorms: number;
  hurricanes: number;
  majorHurricanes: number;
};

/** The storm table and the "Summary as of" line from NHC's season page. */
export function parseSeasonPage(html: string): SeasonToDate {
  const table = html.match(/<tbody>([\s\S]*?)<\/tbody>/)?.[1] ?? "";
  const storms: SeasonToDate["storms"] = [];
  for (const row of table.matchAll(/<tr>([\s\S]*?)<\/tr>/g)) {
    const cells = [...row[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((c) => c[1].replace(/<[^>]+>/g, "").trim());
    if (cells.length < 7 || !/^\d+$/.test(cells[0])) continue;
    storms.push({
      number: Number(cells[0]), name: cells[1].replace(/\*/g, "").trim(), type: cells[2],
      dates: cells[3], maxWindKt: Number(cells[4]), ace: Number(cells[6]) || 0,
    });
  }
  const asOf = html.match(/Summary as of ([^<]+?)\s*<\/th>/)?.[1]?.trim();
  const named = storms.filter((s) => ["TS", "SS", "HU", "MH"].includes(s.type));
  if (!asOf && storms.length === 0) {
    throw new ShapeError("NHC season summary", "a storm table or a 'Summary as of' line", "neither found");
  }
  return {
    asOf, storms,
    namedStorms: named.length,
    hurricanes: named.filter((s) => s.type === "HU" || s.type === "MH").length,
    majorHurricanes: named.filter((s) => s.type === "MH").length,
  };
}

const params = z.object({
  mode: z
    .enum(["seasons", "after_date", "storm_list", "season_to_date"])
    .describe(
      "seasons: per-season counts of named storms, hurricanes, majors and ACE. " +
        "after_date: per-season count of named storms that first reached tropical-storm " +
        "strength after a calendar date (how many more can still form). storm_list: the " +
        "storms of one completed season. season_to_date: the current season's tally from NHC.",
    ),
  basin: z.enum(["atlantic", "pacific"]).optional().describe("atlantic (default) or pacific (eastern + central North Pacific)."),
  from: z.number().int().optional().describe("First season (default 1991; detection is less complete earlier)."),
  to: z.number().int().optional().describe("Last season (default: the newest completed season)."),
  after: z.string().optional().describe("after_date: count storms first qualifying strictly after this UTC month-day, 'MM-DD' (end of that day) or 'MM-DDTHH:MM'."),
  before: z.string().optional().describe("after_date: optional upper bound, same format (inclusive)."),
  season: z.number().int().optional().describe("storm_list / season_to_date: the season year."),
  groupBy: z
    .enum(["enso", "none"])
    .optional()
    .describe("seasons / after_date: means per El Niño / La Niña class of each season, plus the current ENSO state. Default enso; 'none' to skip."),
  ensoIndex: z.enum(["oni", "roni"]).optional().describe("Index for groupBy (default oni)."),
  ensoWindow: z.string().optional().describe("Three-month window for groupBy, e.g. ASO (default) or JAS."),
});

export const nhcHistory = defineAdapter({
  id: "nhc_history",
  name: "NHC Tropical Cyclone History (HURDAT2)",
  tier: "A",
  domain: "weather",
  answers:
    "Counts computed from the National Hurricane Center's official best-track record " +
    "(HURDAT2): named storms, hurricanes, major hurricanes and ACE per past season for " +
    "the Atlantic or eastern/central North Pacific; how many named storms formed after a " +
    "given calendar date in each past season (the base rate for 'how many more this " +
    "season'); those numbers split by El Niño / La Niña state; the storm list of a past " +
    "season; and the current season's running tally from NHC's season summary page. " +
    "NOT for storms active right now (use nhc_storms) or formation chances in the coming " +
    "days (use nhc_outlook).",
  keywords: [
    "hurricane", "hurricanes", "cyclone", "cyclones", "tropical", "named", "hurdat",
    "hurdat2", "nhc", "ace", "typhoon", "subtropical",
  ],
  keywordsRequired: true,
  paramsSchema: params,
  paramsHelp:
    "{mode:'after_date', basin:'atlantic', after:'10-14', from:1991, to:2025} " +
    "for 'how many more storms after mid-October'. {mode:'seasons', from:1991} for season " +
    "totals. {mode:'storm_list', season:2024}. {mode:'season_to_date', season:2026}.",

  async run(input, signal) {
    const basin: Basin = input.basin ?? "atlantic";

    if (input.mode === "season_to_date") {
      const season = input.season ?? new Date().getUTCFullYear();
      const url = `${TCR_URL}?season=${season}&basin=${basin === "atlantic" ? "atl" : "epac"}`;
      const html = await getText(url, { signal, context: "NHC season summary", timeoutMs: 20_000 });
      const found = parseSeasonPage(html);
      return {
        tier: "A" as const,
        url,
        retrievedAt: now(),
        rows: [
          { season, asOf: found.asOf, namedStorms: found.namedStorms, hurricanes: found.hurricanes, majorHurricanes: found.majorHurricanes },
          ...found.storms.slice(0, 24),
        ],
        truncated: found.storms.length > 24,
        note:
          "SEASON-TO-DATE and preliminary, as of the time shown — not the season's final " +
          "count. Named = TS, SS, HU or MH rows; TD/SD/PTC rows are listed but not counted. " +
          "Post-season reanalysis can still add or remove borderline storms.",
      };
    }

    const { storms, url } = await loadStorms(basin, signal);
    const lastComplete = Math.max(...storms.map((s) => s.season));
    const from = input.from ?? 1991;
    const to = Math.min(input.to ?? lastComplete, lastComplete);
    const seasons = Array.from({ length: Math.max(0, to - from + 1) }, (_, i) => from + i);
    const named = storms.filter((s) => s.qualified);
    const baseNote =
      `Computed from ${url.split("/").pop()} (post-season best track, seasons through ` +
      `${lastComplete}). Named = first six-hourly point as TS/SS/HU with >= 34 kt (UTC), ` +
      "so unnamed subtropical storms added after a season count, as they do in NHC's totals. " +
      "Satellite-era detection makes counts before ~1991 lower than comparable modern ones. ";

    if (input.mode === "storm_list") {
      const season = input.season ?? lastComplete;
      const list = named.filter((s) => s.season === season);
      return {
        tier: "A" as const, url, retrievedAt: now(),
        rows: list.slice(0, 25).map((s) => ({
          id: s.id, name: s.name, qualifiedUtc: s.qualified, peakWindKt: s.peakWind,
          hurricane: s.hurricane, major: s.major, ace: Number(s.ace.toFixed(1)),
        })),
        truncated: list.length > 25,
        note: baseNote + (season > lastComplete ? `Season ${season} is not in HURDAT2 yet; use season_to_date.` : ""),
      };
    }

    let perSeason: SeasonStat[];
    let label: string;
    let extra: Record<string, unknown> = {};
    if (input.mode === "after_date") {
      if (!input.after) throw new Error("after_date needs `after`, e.g. '10-14'");
      const lo = monthDayKey(input.after, true);
      const hi = input.before ? monthDayKey(input.before, true) : "12312359";
      perSeason = seasons.map((season) => ({
        season,
        value: named.filter((s) => s.season === season && s.qualified!.slice(4) > lo && s.qualified!.slice(4) <= hi).length,
      }));
      label = `named storms first qualifying after ${input.after} UTC${input.before ? ` and by ${input.before}` : ""}`;
    } else {
      const totals = seasons.map((season) => {
        const list = named.filter((s) => s.season === season);
        return {
          season, named: list.length, hurricanes: list.filter((s) => s.hurricane).length,
          majors: list.filter((s) => s.major).length, ace: Number(list.reduce((a, s) => a + s.ace, 0).toFixed(1)),
        };
      });
      perSeason = totals.map((t) => ({ season: t.season, value: t.named }));
      label = "named storms per season";
      extra = {
        hurricanes: summarise(totals.map((t) => ({ season: t.season, value: t.hurricanes }))),
        majors: summarise(totals.map((t) => ({ season: t.season, value: t.majors }))),
        ace: summarise(totals.map((t) => ({ season: t.season, value: t.ace }))),
        perSeasonDetail: totals.length <= 12
          ? totals.map((t) => `${t.season}: ${t.named}/${t.hurricanes}/${t.majors} ACE ${t.ace}`).join("; ")
          : undefined,
      };
    }

    const rows: unknown[] = [{
      measure: label, basin, seasonsFrom: from, seasonsTo: to, ...summarise(perSeason),
      perSeason: compact(perSeason), ...extra,
    }];
    // ENSO grouping is on by default: on 46147 the agent never asked for it, and
    // the El Niño-conditioned remainder (0.6-0.9 storms vs 2.1 overall) was the
    // single most decisive number. One failing ENSO fetch only drops these rows.
    const grouped = input.groupBy !== "none";
    let ensoRows: unknown[] = [];
    if (grouped) {
      try {
        ensoRows = [
          ...await groupByEnso(perSeason, input.ensoIndex ?? "oni", (input.ensoWindow ?? "ASO").toUpperCase(), signal),
          await currentEnso(signal),
        ];
      } catch (error) {
        ensoRows = [{ group: "ENSO split unavailable", error: String((error as Error)?.message ?? error) }];
      }
    }
    rows.push(...ensoRows);
    return {
      tier: "A" as const, url, retrievedAt: now(), rows,
      note:
        baseNote +
        (grouped
          ? `ENSO classes use CPC ${(input.ensoIndex ?? "oni").toUpperCase()} rounded to one decimal; ` +
            "buckets are alternative reference classes (the El Niño row includes the strong one), " +
            "not independent evidence to add together. Small classes (under ~8 seasons) are fragile. "
          : "") +
        "These are historical frequencies for comparable seasons, not a forecast for this one.",
    };
  },
});

