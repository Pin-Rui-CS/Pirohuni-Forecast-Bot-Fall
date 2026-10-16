/**
 * The API matcher on every real need the agent has written (test/fixtures/
 * needs.json: 101 question titles + 264 find_apis needs from stored runs).
 *
 * Two layers:
 *   1. Rules by label: tropical needs must be offered an NHC adapter; "other"
 *      needs must never be offered nhc_* or cpc_*.
 *   2. A reviewed snapshot (find-expected.json): ANY change to any adapter's
 *      candidates for any real need fails, with the scoring explained, so a new
 *      adapter or keyword can never quietly re-route unrelated questions.
 *      After reviewing an intended change: node test/find-snapshot.ts > test/fixtures/find-expected.json
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

process.env.FRED_API_KEY ??= "test";
process.env.METACULUS_API_TOKEN ??= "test";
const { find, explainScore } = await import("../src/core/registry.ts");

type Need = { text: string; label: "tropical" | "other" | "enso_ok" | "topic_ok" };
const needs: Need[] = JSON.parse(readFileSync(new URL("./fixtures/needs.json", import.meta.url), "utf8"));
const expected: Record<string, string[]> = JSON.parse(
  readFileSync(new URL("./fixtures/find-expected.json", import.meta.url), "utf8"),
);
const ids = (need: string) => find(need).map((c: { id: string }) => c.id);
const NARROW = /^(nhc|cpc)_/;

test("tropical-cyclone needs are offered an NHC adapter", () => {
  const misses = needs.filter((n) => n.label === "tropical" && !ids(n.text).some((id) => id.startsWith("nhc_")));
  assert.deepEqual(misses.map((n) => n.text), []);
});

test("no other real need is offered a tropical or ENSO adapter", () => {
  const offers = needs
    .filter((n) => n.label === "other")
    .flatMap((n) => ids(n.text).filter((id) => NARROW.test(id)).map((id) => `${id} <- ${n.text} ${JSON.stringify(explainScore(n.text, id).hits)}`));
  assert.deepEqual(offers, []);
});

test("candidate lists match the reviewed snapshot", () => {
  const changed = needs
    .filter((n) => JSON.stringify(ids(n.text)) !== JSON.stringify(expected[n.text]))
    .map((n) => `${n.text}\n   expected ${JSON.stringify(expected[n.text])}\n   got      ${JSON.stringify(ids(n.text))}`);
  assert.deepEqual(changed, []);
});

test("hand-written probes route to the right adapter", () => {
  const cases: Array<[string, string]> = [
    ["historical named storm counts after mid-October by El Niño year", "nhc_history"],
    ["HURDAT2 best track Atlantic hurricane season totals", "nhc_history"],
    ["tropical cyclone formation chances next week Atlantic", "nhc_outlook"],
    ["El Niño strength probability for winter", "cpc_enso"],
    ["El Nino strength probability for winter", "cpc_enso"],
    ["ONI value for ASO 2023", "cpc_enso"],
  ];
  for (const [need, id] of cases) assert.ok(ids(need).includes(id), `${need} -> ${JSON.stringify(ids(need))}`);
  for (const need of [
    "NBA 2026 regular season wins historical base rate",
    "Federal Reserve rate outlook December",
    "Denver Colorado weather forecast for game day",
    "TOP500 supercomputer list latest systems",
    "magnitude 6 earthquakes historical count",
    "CDC travel advisory list",
  ]) {
    assert.deepEqual(ids(need).filter((id) => NARROW.test(id)), [], need);
  }
});
