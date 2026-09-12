/**
 * The toolkit with no LLM anywhere.
 *
 *   node --env-file-if-exists=.env examples/tools-only.ts
 *
 * This is the fastest way to confirm the package works after copying it in: it
 * needs no model, no LLM credentials, and no paid anything. It exercises the
 * deterministic matcher and then makes a real call against a keyless Tier A
 * endpoint.
 *
 * It is also the shape to copy if you are wiring these tools into an agent you
 * already have: get `definitions`, hand them to your model, route what comes
 * back through `dispatch`.
 */
import { createToolkit } from "../src/index.ts";

const toolkit = createToolkit();

console.log("Tools exposed to the model:");
for (const definition of toolkit.definitions) {
  console.log(`  ${definition.name}`);
}

// 1. Which APIs could answer this? Deterministic — no model, no network.
const need = "How many US banks failed in 2026?";
console.log(`\nfind_apis: ${JSON.stringify(need)}`);

const shortlist = (await toolkit.findApis({ need })) as {
  candidates: Array<{ id: string; name: string; tier: string }>;
};

for (const candidate of shortlist.candidates) {
  console.log(`  [${candidate.tier}] ${candidate.id.padEnd(22)} ${candidate.name}`);
}

// 2. Actually call the best one. FDIC needs no credential.
console.log("\ncall_api: fdic_failures");

const result = (await toolkit.callApi({
  api: "fdic_failures",
  params: { from: "2026-01-01", to: "2026-12-31" },
})) as Record<string, unknown>;

if (result.error) {
  console.log(`  FAILED: ${result.error}`);
  if (result.paramsHelp) console.log(`  expects: ${result.paramsHelp}`);
} else {
  console.log(`  tier ${result.tier}, ${result.rowCount} rows`);
  console.log(`  source: ${result.url}`);
  console.log(`  retrieved: ${result.retrievedAt}`);
  console.log(`\n${JSON.stringify(result.rows, null, 2).slice(0, 1200)}`);
}

console.log(`\nusage: ${JSON.stringify(toolkit.usage())}`);
