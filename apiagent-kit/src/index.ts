/**
 * apiagent-kit — a research agent over 24 public data APIs.
 *
 * Three layers, each usable without the ones above it:
 *
 *   runAgent()        the tuned loop: forced lookup, free middle, denied end
 *   createToolkit()   the two tools, provider-neutral — bring your own loop
 *   find/getAdapter   the registry itself — bring your own everything
 *
 * Nothing here imports an LLM SDK. `zod` is the only runtime dependency.
 */

/* --- The agent ----------------------------------------------------------- */
export { runAgent, MAX_STEPS } from "./agent/loop.ts";
export type { AgentEvent, AgentResult, RunAgentOptions } from "./agent/loop.ts";
export { systemPrompt, lastStepPrompt } from "./agent/prompts.ts";

/* --- The model seam ------------------------------------------------------ */
export type {
  ChatMessage,
  ChatModel,
  ChatRequest,
  ChatResponse,
  TokenUsage,
  ToolCall,
  ToolChoice,
  ToolResult,
} from "./agent/model.ts";
export { openAICompatible } from "./models/openai-compatible.ts";
export type { OpenAICompatibleOptions } from "./models/openai-compatible.ts";

/* --- The tools ----------------------------------------------------------- */
export {
  createToolkit,
  toolDefinitions,
  explain,
  TOOL_NAMES,
} from "./toolkit.ts";
export type {
  Toolkit,
  ToolDefinition,
  ToolName,
  TurnUsage,
} from "./toolkit.ts";

/* --- The registry -------------------------------------------------------- */
export { adapters, activeAdapters, getAdapter, find } from "./core/registry.ts";
export type { Candidate } from "./core/registry.ts";
export { defineAdapter } from "./core/types.ts";
export type {
  AdapterResult,
  AdapterSpec,
  ApiAdapter,
  Domain,
  Tier,
} from "./core/types.ts";

/* --- Building your own adapter ------------------------------------------- */
export {
  HttpError,
  ShapeError,
  assertShape,
  capRows,
  csvToObjects,
  getJson,
  getText,
  isObject,
  now,
  parseCsv,
  spaced,
  truncate,
  userAgent,
  MAX_RESULT_CHARS,
  MAX_ROWS,
  REQUEST_TIMEOUT_MS,
  TURN_CHAR_BUDGET,
} from "./core/http.ts";
