import type { ToolDefinition } from "../toolkit.ts";

/**
 * The seam between this package and your LLM.
 *
 * Everything else here is provider-neutral, and this interface is why. Implement
 * `complete` against whatever you already use — the official Anthropic SDK, the
 * OpenAI SDK, the Vercel AI SDK, a gateway, a local model — and the loop in
 * `loop.ts` runs unchanged. One reference implementation ships in
 * `../models/openai-compatible.ts`; the README shows an Anthropic one.
 *
 * Deliberately small. It asks for one non-streaming completion that may contain
 * tool calls, because that is the whole of what the research loop needs. If you
 * want token streaming, stream inside your own implementation and resolve the
 * promise when the turn is complete — the loop only reads the final result.
 */

/** A tool call the model wants performed. */
export type ToolCall = {
  /**
   * Provider-assigned id. Echoed back with the result so the model can match
   * them up when several run in parallel. Synthesise one if your provider does
   * not supply it.
   */
  id: string;
  name: string;
  /** Already JSON-parsed. Never hand the loop a raw string. */
  arguments: unknown;
};

/** The result of running one tool call. */
export type ToolResult = {
  callId: string;
  name: string;
  /** Whatever the toolkit returned, serialised by the model implementation. */
  output: unknown;
  isError: boolean;
};

export type ChatMessage =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; toolCalls: ToolCall[] }
  | { role: "tool"; results: ToolResult[] };

/**
 * How free the model is to call a tool on this step.
 *
 *  auto      — its choice.
 *  required  — it must call at least one tool.
 *  none      — tools are visible but calling one is refused.
 *
 * `loop.ts` uses all three; see the phase comment there for why. If your
 * provider cannot force a call, map `required` to `auto` and add an instruction
 * naming the tool — say so in your implementation rather than silently
 * downgrading, because the loop's first two steps depend on it.
 */
export type ToolChoice = "auto" | "required" | "none";

export type ChatRequest = {
  /** Standing instructions. The loop swaps this on the final step. */
  system: string;
  messages: ChatMessage[];
  tools: ToolDefinition[];
  toolChoice: ToolChoice;
  signal?: AbortSignal;
};

export type TokenUsage = {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
};

export type ChatResponse = {
  /** Visible text. May be empty on a step that only called tools. */
  text: string;
  toolCalls: ToolCall[];
  /** Provider's own reason string, passed through untouched for logging. */
  finishReason?: string;
  usage?: TokenUsage;
};

export interface ChatModel {
  /** Identifier for logs and provenance. */
  readonly id: string;
  complete(request: ChatRequest): Promise<ChatResponse>;
}
