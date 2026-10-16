import { ShapeError, htmlToText, truncate } from "../http.ts";

/**
 * NOAA Climate Prediction Center — Global Tropics Hazards Outlook (weeks 2-3).
 *
 * The only official product looking past NHC's 7-day outlook for tropical
 * cyclone formation. The map is an image, but the page carries the written
 * discussion inline with its update date and validity window. This module
 * parses those and the sentences about tropical cyclones and each basin; the
 * `nhc_outlook` adapter serves them as its weeks 2-3 half (one formation-outlook
 * adapter rather than two keeps hurricane needs from crowding out other APIs).
 *
 * Verified 2026-10-15: "Last Updated - 10/13/26 Valid - 10/21/26 - 11/03/26".
 */

export const HAZARDS_URL = "https://www.cpc.ncep.noaa.gov/products/precip/CWlink/ghazards/";

const BASIN_TERMS: Record<"atlantic" | "pacific", RegExp> = {
  // "the Gulf" alone means the Gulf of America here, but not "the Gulf of California".
  atlantic: /\b(Atlantic|Caribbean|Gulf of (?:Mexico|America)|the Gulf(?! of))\b/i,
  pacific: /\b(Pacific|EPAC|south(?:west)? of Mexico|Baja|Gulf of California|JTWC|typhoons?)\b/i,
};
const TC_TERMS = /\b(tropical cyclones?|TCs?|hurricanes?|typhoons?|tropical storms?|(?:sub)?tropical development|development)\b/i;
const MAX_SENTENCES = 10;

export type Hazards = { lastUpdated?: string; valid?: string; sentences: string[] };

/**
 * Sentences are chosen by relevance, not page order (the Atlantic paragraph
 * usually comes last): sentences naming the requested basin with a tropical-
 * cyclone term first, each followed by the next sentence when it continues the
 * thought without naming another basin ("However, no TC-related areas are
 * posted..."); then basin-neutral tropical-cyclone sentences. Sentences only
 * about the other basin are dropped. Output keeps page order.
 */
export function parseHazards(html: string, basin: "atlantic" | "pacific" | "all"): Hazards {
  const flat = htmlToText(html);
  const header = flat.match(/GTH Outlook Discussion\s+Last Updated\s*-\s*([\d/]+)\s+Valid\s*-\s*([\d/]+\s*-\s*[\d/]+)/i);
  if (!header) {
    throw new ShapeError("CPC Global Tropics Hazards", "a dated 'GTH Outlook Discussion' section", truncate(flat, 200));
  }
  const discussion = flat.slice(flat.indexOf(header[0]) + header[0].length);
  const sentences = (discussion.match(/[^.!?]+[.!?]/g) ?? []).map((s) => s.trim());
  const isTc = (s: string) => TC_TERMS.test(s);
  const names = (s: string, b: "atlantic" | "pacific") => BASIN_TERMS[b].test(s);

  let chosen: number[];
  if (basin === "all") {
    chosen = sentences.map((s, i) => (isTc(s) ? i : -1)).filter((i) => i >= 0).slice(0, MAX_SENTENCES);
  } else {
    const other = basin === "atlantic" ? "pacific" : "atlantic";
    const primary = new Set<number>();
    sentences.forEach((s, i) => {
      if (!names(s, basin) || !isTc(s)) return;
      primary.add(i);
      const next = sentences[i + 1];
      if (next && !names(next, other) && (isTc(next) || /^(However|But|Therefore|Thus)\b/.test(next))) primary.add(i + 1);
    });
    const neutral = sentences
      .map((s, i) => (isTc(s) && !names(s, basin) && !names(s, other) && !primary.has(i) ? i : -1))
      .filter((i) => i >= 0);
    chosen = [...primary, ...neutral].slice(0, MAX_SENTENCES).sort((a, b) => a - b);
  }
  return {
    lastUpdated: header[1],
    valid: header[2].replace(/\s+/g, " "),
    sentences: chosen.map((i) => sentences[i]),
  };
}
