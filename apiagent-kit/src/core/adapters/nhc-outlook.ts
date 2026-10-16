import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { ShapeError, getText, now, truncate } from "../http.ts";
import { HAZARDS_URL, parseHazards } from "./cpc-hazards.ts";

/**
 * National Hurricane Center — Tropical Weather Outlook (formation chances).
 *
 * The outlook is NHC's statement of where new tropical cyclones might form in
 * the next 2 and 7 days. It is a FORECAST, which is why it is a separate
 * adapter from `nhc_storms` (what exists now) and `nhc_history` (what happened).
 *
 * Read from the plain-text bulletin on the NWS file server rather than the web
 * page: same words, no HTML to break.
 *
 * Verified 2026-10-15: Atlantic 800 PM EDT Wed Oct 14 2026 "Tropical cyclone
 * formation is not expected during the next 7 days."; eastern Pacific 500 PM
 * PDT with two active systems and one area at 20% in 7 days.
 */

const BULLETINS = {
  atlantic: "https://tgftp.nws.noaa.gov/data/raw/ab/abnt20.knhc.two.at.txt",
  pacific: "https://tgftp.nws.noaa.gov/data/raw/ab/abpz20.knhc.two.ep.txt",
} as const;

export type Chance = { category: string; percent: number };
export type Area = { area: string; chance48h?: Chance; chance7d?: Chance; text: string };
export type Outlook = {
  issued?: string;
  region?: string;
  activeSystems?: string;
  areas: Area[];
  formationNotExpected: boolean;
  forecaster?: string;
};

function chance(text: string, window: "48 hours" | "7 days"): Chance | undefined {
  const match = text.match(
    new RegExp(`Formation chance through ${window}\\.+\\s*(\\w+)\\.+\\s*(?:near\\s+)?(\\d+)\\s*percent`, "i"),
  );
  return match ? { category: match[1].toLowerCase(), percent: Number(match[2]) } : undefined;
}

export function parseOutlook(text: string): Outlook {
  const lines = text.split(/\r?\n/);
  const at = lines.findIndex((line) => /National Hurricane Center/i.test(line));
  const issued = at >= 0 ? lines[at + 1]?.trim() : undefined;
  const regionLine = lines.find((line) => /^For the /.test(line.trim()));
  const start = regionLine ? lines.indexOf(regionLine) + 1 : 0;
  const end = lines.findIndex((line, i) => i > start && line.trim() === "$$");
  const body = lines.slice(start, end > 0 ? end : undefined).join("\n");
  const forecaster = lines.slice(end > 0 ? end : 0).find((line) => /^Forecaster/i.test(line.trim()))?.trim();
  const paragraphs = body.split(/\n\s*\n/).map((p) => p.replace(/\s+/g, " ").trim()).filter(Boolean);

  const areas: Area[] = [];
  let activeSystems: string | undefined;
  for (const paragraph of paragraphs) {
    if (/^Active Systems:/i.test(paragraph)) {
      activeSystems = paragraph.replace(/^Active Systems:\s*/i, "");
      continue;
    }
    const heading = paragraph.match(/^((?:\d+\.\s*)?[^.:]{3,90}):\s/);
    const c48 = chance(paragraph, "48 hours");
    const c7 = chance(paragraph, "7 days");
    if (heading && (c48 || c7)) {
      areas.push({ area: heading[1].trim(), chance48h: c48, chance7d: c7, text: truncate(paragraph, 500) });
    }
  }
  const formationNotExpected = /formation is not expected during the next 7 days/i.test(body);
  if (!issued && areas.length === 0 && !formationNotExpected) {
    throw new ShapeError("NHC Tropical Weather Outlook", "an issuance line and areas or a no-formation line", truncate(text, 200));
  }
  return { issued, region: regionLine?.trim().replace(/:$/, ""), activeSystems, areas, formationNotExpected, forecaster };
}

const params = z.object({
  basin: z.enum(["atlantic", "pacific"]).optional().describe("atlantic (default) or pacific (eastern + central North Pacific)."),
  range: z
    .enum(["7day", "weeks2-3", "both"])
    .optional()
    .describe("7day: NHC's outlook. weeks2-3: CPC's Global Tropics Hazards discussion (1-3 weeks out). Default both."),
});

export const nhcOutlook = defineAdapter({
  id: "nhc_outlook",
  name: "Tropical Cyclone Formation Outlook (NHC 7-day + CPC weeks 2-3)",
  tier: "A",
  domain: "weather",
  answers:
    "Official tropical cyclone formation outlooks. Next 7 days: the National Hurricane " +
    "Center's Tropical Weather Outlook, with each area being watched and its chance of " +
    "forming in 48 hours and 7 days, or NHC's statement that formation is not expected, " +
    "plus systems with active advisories. Weeks 2-3: the dated validity window and the " +
    "tropical-cyclone sentences of NOAA CPC's Global Tropics Hazards discussion. Atlantic " +
    "or eastern Pacific. Use it for 'could a new tropical storm or hurricane form soon or " +
    "in the next few weeks'. NOT a record of past storms (nhc_history).",
  keywords: [
    "hurricane", "hurricanes", "cyclone", "cyclones", "tropical", "formation", "genesis",
    "disturbance", "nhc", "invest", "typhoon", "subseasonal", "mjo",
  ],
  keywordsRequired: true,
  paramsSchema: params,
  paramsHelp: "{basin:'atlantic'} (default, both ranges), {basin:'pacific'}, or range:'7day'|'weeks2-3'.",

  async run(input, signal) {
    const basin = input.basin ?? "atlantic";
    const range = input.range ?? "both";
    const [nhcPart, cpcPart] = await Promise.allSettled([
      range === "weeks2-3" ? Promise.resolve(null)
        : getText(BULLETINS[basin], { signal, context: "NHC Tropical Weather Outlook", timeoutMs: 20_000 }).then(parseOutlook),
      range === "7day" ? Promise.resolve(null)
        : getText(HAZARDS_URL, { signal, context: "CPC Global Tropics Hazards", timeoutMs: 20_000 })
          .then((html) => parseHazards(html, basin)),
    ]);
    // One failing half must not hide the other; if both fail, surface the first error.
    if (nhcPart.status === "rejected" && (cpcPart.status === "rejected" || range === "7day")) throw nhcPart.reason;
    if (cpcPart.status === "rejected" && range === "weeks2-3") throw cpcPart.reason;

    const rows: unknown[] = [];
    if (nhcPart.status === "fulfilled" && nhcPart.value) {
      const outlook = nhcPart.value;
      rows.push(
        { source: "NHC 7-day", issued: outlook.issued, region: outlook.region,
          formationNotExpected: outlook.formationNotExpected,
          activeSystems: outlook.activeSystems ? truncate(outlook.activeSystems, 400) : undefined },
        ...outlook.areas.map((area) => ({ source: "NHC 7-day", ...area })),
      );
    } else if (nhcPart.status === "rejected") {
      rows.push({ source: "NHC 7-day", error: String(nhcPart.reason?.message ?? nhcPart.reason) });
    }
    if (cpcPart.status === "fulfilled" && cpcPart.value) {
      const hz = cpcPart.value;
      rows.push({ source: "CPC weeks 2-3", lastUpdated: hz.lastUpdated, valid: hz.valid },
        ...hz.sentences.map((sentence) => ({ source: "CPC weeks 2-3", sentence })));
    } else if (cpcPart.status === "rejected") {
      rows.push({ source: "CPC weeks 2-3", error: String(cpcPart.reason?.message ?? cpcPart.reason) });
    }
    return {
      tier: "A" as const,
      url: range === "weeks2-3" ? HAZARDS_URL : BULLETINS[basin],
      retrievedAt: now(),
      rows: rows.slice(0, 25),
      truncated: rows.length > 25,
      note:
        "FORECASTS. NHC's outlook covers 7 days and is issued every 6 hours (check `issued`); " +
        "CPC's weeks 2-3 discussion is weekly (Tuesdays) and its sentences are keyword-selected " +
        "excerpts. 'Not expected' or no mention is not zero chance for the rest of the season, " +
        "and a percent is a formation probability, not a probability of landfall or of any count.",
    };
  },
});
