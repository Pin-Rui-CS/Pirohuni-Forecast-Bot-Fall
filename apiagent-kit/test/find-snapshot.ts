/**
 * Print find() candidates for every entry of test/fixtures/needs.json, as JSON.
 * Used once to record the pre-change baseline (fixtures/find-baseline.json);
 * the matcher test compares the live matcher against it.
 */
process.env.FRED_API_KEY ??= "test";
process.env.METACULUS_API_TOKEN ??= "test";
const { find } = await import("../src/core/registry.ts");
const { readFileSync } = await import("node:fs");
const needs = JSON.parse(readFileSync(new URL("./fixtures/needs.json", import.meta.url), "utf8"));
const out: Record<string, string[]> = {};
for (const entry of needs) out[entry.text] = find(entry.text).map((c: { id: string }) => c.id);
console.log(JSON.stringify(out, null, 1));
