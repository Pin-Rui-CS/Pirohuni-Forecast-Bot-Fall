/**
 * The whole agent, end to end.
 *
 *   node --env-file-if-exists=.env examples/ask.ts "How many US banks failed in August 2026?"
 *
 * Needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL. If you only want to see the
 * APIs work, run examples/tools-only.ts instead — it needs no model at all.
 */
import { openAICompatible, runAgent } from "../src/index.ts";

const question = process.argv.slice(2).join(" ").trim();
if (!question) {
  console.error('Usage: node examples/ask.ts "your question"');
  process.exit(1);
}

const { LLM_BASE_URL, LLM_API_KEY, LLM_MODEL } = process.env;
if (!LLM_BASE_URL || !LLM_API_KEY || !LLM_MODEL) {
  console.error(
    "Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (see .env.example).",
  );
  process.exit(1);
}

const model = openAICompatible({
  baseUrl: LLM_BASE_URL,
  apiKey: LLM_API_KEY,
  model: LLM_MODEL,
});

// Ctrl-C aborts the in-flight request rather than leaving it running.
const controller = new AbortController();
process.on("SIGINT", () => controller.abort());

const result = await runAgent({
  model,
  question,
  signal: controller.signal,
  onEvent: (event) => {
    if (event.type === "step-start") {
      console.error(`\n[step ${event.step}] tools: ${event.toolChoice}`);
    }
    if (event.type === "tool-call") {
      const input = JSON.stringify(event.call.arguments);
      console.error(`  -> ${event.call.name} ${truncate(input, 120)}`);
    }
    if (event.type === "tool-result") {
      const mark = event.result.isError ? "FAIL" : "ok";
      console.error(`  <- ${mark} ${event.result.name} (${event.elapsedMs}ms)`);
    }
  },
});

console.log(`\n${result.text}\n`);
console.error(
  `— ${result.steps} steps, ${result.usage.lookups} lookups, ` +
    `${result.usage.calls} calls, ${result.usage.failures} failures, ` +
    `tiers ${result.usage.tiersUsed.join("/") || "none"}, ` +
    `${result.tokens.totalTokens ?? "?"} tokens, ended: ${result.reason}`,
);

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
