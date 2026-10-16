/**
 * Parsers of the tropical / ENSO adapters, on fixtures saved from the live
 * sources (2026-10-15) plus small hand-built HURDAT2 / ONI snippets.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { monthDayKey, newestFile, parseHurdat2, parseSeasonPage } from "../src/core/adapters/nhc-history.ts";
import { parseOutlook } from "../src/core/adapters/nhc-outlook.ts";
import { parseHazards } from "../src/core/adapters/cpc-hazards.ts";
import { ensoClass, parseDiscussion, parseIndexTable, roundCpc } from "../src/core/adapters/cpc-enso.ts";

const fixture = (name: string) => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8");

const track = (date: string, time: string, status: string, wind: number) =>
  `${date}, ${time},  , ${status}, 30.0N,  57.6W, ${String(wind).padStart(3)}, 1000,    0`;

/** 120 filler storms (the parser rejects files with under 100) + the cases under test. */
function hurdat(extra: string[]): string {
  const filler: string[] = [];
  for (let i = 0; i < 120; i += 1) {
    filler.push(`AL${String((i % 30) + 1).padStart(2, "0")}1990,            FILLER,      1,`, track("19900801", "0000", "TD", 30));
  }
  return [...filler, ...extra].join("\n");
}

test("HURDAT2: qualifying moment, SS counts, TD-only does not, ACE and categories", () => {
  const storms = parseHurdat2(hurdat([
    "AL012025,             ANDREA,      4,",
    track("20250622", "0000", "LO", 25),
    track("20250623", "0600", "TD", 30),
    track("20250624", "0000", "TS", 35),
    track("20250624", "0600", "TS", 40),
    "AL022025,              BARRY,      2,",
    track("20251015", "1200", "SS", 45),
    track("20251016", "0000", "HU", 100),
    "AL032025,           DEPRESS,      2,",
    track("20251020", "0000", "TD", 30),
    track("20251020", "0600", "TD", 33),
  ]));
  const by = Object.fromEntries(storms.filter((s) => s.season === 2025).map((s) => [s.name, s]));
  assert.equal(by.ANDREA.qualified, "202506240000");
  assert.equal(by.ANDREA.ace.toFixed(4), ((35 * 35 + 40 * 40) / 1e4).toFixed(4));
  assert.equal(by.BARRY.qualified, "202510151200");           // subtropical storm counts
  assert.equal(by.BARRY.major, true);
  assert.equal(by.DEPRESS.qualified, undefined);              // never reached 34 kt
});

test("HURDAT2: month-day cutoffs compare in UTC", () => {
  assert.equal(monthDayKey("10-14", true), "10142359");
  assert.equal(monthDayKey("10-14T05:05", false), "10140505");
  assert.ok("10140600" > monthDayKey("10-14T05:05", true));
  assert.throws(() => monthDayKey("Oct 14", true));
});

test("HURDAT2 index: newest file per basin by listed date, not by name", () => {
  const index = fixture("hurdat-index.html");
  assert.equal(newestFile(index, "atlantic"), "hurdat2-1851-2025-092326.txt");
  assert.equal(newestFile(index, "pacific"), "hurdat2-nepac-1949-2025-092926.txt");
});

test("NHC season page: named storms counted, summary line read", () => {
  const page = parseSeasonPage(fixture("tcr-2026-atl.html"));
  assert.equal(page.namedStorms, 9);
  assert.equal(page.hurricanes, 1);
  assert.equal(page.majorHurricanes, 1);
  assert.equal(page.storms.at(-1)?.name, "Isaias");
  assert.match(page.asOf ?? "", /15 October 2026/);
});

test("Tropical Weather Outlook: 'not expected' and an area with chances", () => {
  const none = parseOutlook(fixture("two-atl-none.txt"));
  assert.equal(none.formationNotExpected, true);
  assert.equal(none.areas.length, 0);
  assert.match(none.issued ?? "", /Oct 14 2026/);

  const ep = parseOutlook(fixture("two-ep-area.txt"));
  assert.equal(ep.formationNotExpected, false);
  assert.match(ep.activeSystems ?? "", /Hurricane Simon/);
  assert.equal(ep.areas.length, 1);
  assert.equal(ep.areas[0].area, "South of Southern Mexico");
  assert.deepEqual(ep.areas[0].chance48h, { category: "low", percent: 0 });
  assert.deepEqual(ep.areas[0].chance7d, { category: "low", percent: 20 });
});

test("CPC weeks 2-3 hazards: dated window and tropical-cyclone sentences", () => {
  const hz = parseHazards(fixture("gth.html"), "atlantic");
  assert.equal(hz.lastUpdated, "10/13/26");
  assert.equal(hz.valid, "10/21/26 - 11/03/26");
  // The decisive Atlantic sentences come last on the page and must survive selection.
  assert.ok(hz.sentences.some((s) => /subtropical Atlantic where tools maintain modest chances/.test(s)), hz.sentences.join("\n"));
  assert.ok(hz.sentences.some((s) => /no TC-related areas are posted/.test(s)));
  assert.ok(hz.sentences.some((s) => /Gulf in the Atlantic basin for week-2/.test(s)));
  assert.ok(!hz.sentences.some((s) => /JTWC/.test(s)));     // western Pacific only: dropped
  assert.ok(hz.sentences.length <= 10);
});

test("ONI: CPC rounding decides the class (1982 ASO 1.45 -> 1.5)", () => {
  assert.equal(roundCpc(1.45), 1.5);
  assert.equal(roundCpc(1.49), 1.5);
  assert.equal(roundCpc(0.47), 0.5);
  assert.equal(roundCpc(-0.45), -0.5);
  assert.equal(roundCpc(0.44), 0.4);
  assert.equal(ensoClass(1.5), "strong El Niño (>= +1.5)");
  assert.equal(ensoClass(0.4), "neutral");
  const lines = [" SEAS  YR   TOTAL   ANOM"];
  for (let year = 1950; year < 1960; year += 1) {
    for (const season of ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]) {
      lines.push(`  ${season} ${year}  26.00  ${season === "ASO" ? "1.45" : "0.10"}`);
    }
  }
  const rows = parseIndexTable(lines.join("\n"), "test");
  assert.equal(rows.length, 120);
  assert.equal(rows.find((r) => r.season === "ASO")?.rounded, 1.5);
  assert.throws(() => parseIndexTable("<html>error</html>", "test"));
});

test("ENSO discussion: issue date, alert status, synopsis, probabilities", () => {
  const d = parseDiscussion(fixture("ensodisc.html"));
  assert.equal(d.issued, "8 October 2026");
  assert.equal(d.alertStatus, "El Niño Advisory");
  assert.match(d.synopsis ?? "", /strong-to-very strong El Niño likely/);
  assert.ok(d.probabilityStatements.some((s) => /83% for October-December/.test(s)));
});
