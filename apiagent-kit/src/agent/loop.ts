import { createToolkit, type Toolkit, type TurnUsage } from "../toolkit.ts";
import { lastStepPrompt, systemPrompt } from "./prompts.ts";
import type {
  ChatMessage,
  ChatModel,
  ChatResponse,
  ToolCall,
  ToolChoice,
  ToolResult,
  TokenUsage,
} from "./model.ts";

/**
 * The research loop.
 *
 * Provider-agnostic: it drives a `ChatModel` and the toolkit, and owns the two
 * decisions that make this agent work — how many steps it gets, and when it is
 * allowed to call a tool.
 */

/**
 * Six.
 *
 * The floor for this design is find_apis -> call_api -> call_api -> answer,
 * which is already four with no room to correct a bad parameter. Six leaves one
 * recovery and one follow-up lookup.
 *
 * It is not higher because every step is another request against whatever rate
 * limit your gateway enforces, and because all six share one wall clock with
 * the answer itself — and one of the adapters, GDELT, deliberately waits before
 * it will call out at all.
 */
export const MAX_STEPS = 6;

export type AgentEvent =
  | { type: "step-start"; step: number; toolChoice: ToolChoice }
  | { type: "tool-call"; step: number; call: ToolCall }
  | { type: "tool-result"; step: number; result: ToolResult; elapsedMs: number }
  | { type: "step-end"; step: number; text: string; usage?: TokenUsage }
  | { type: "done"; reason: "answered" | "steps-exhausted" };

export type RunAgentOptions = {
  model: ChatModel;
  /** The question. Ignored when `messages` is supplied. */
  question?: string;
  /** Full history, for multi-turn callers. Must end with a user message. */
  messages?: ChatMessage[];
  /** Reuse a toolkit to share a data budget across calls. Default: fresh. */
  toolkit?: Toolkit;
  maxSteps?: number;
  /** Injected into the prompt. Override only for reproducible tests. */
  today?: Date;
  /** Observability. Fired as the turn runs; never affects control flow. */
  onEvent?: (event: AgentEvent) => void;
  signal?: AbortSignal;
};

export type AgentResult = {
  /** The final answer. Empty only if the model never produced text. */
  text: string;
  /** Full transcript including tool traffic, for a follow-up turn. */
  messages: ChatMessage[];
  steps: number;
  usage: TurnUsage;
  tokens: TokenUsage;
  reason: "answered" | "steps-exhausted";
};

export async function runAgent(options: RunAgentOptions): Promise<AgentResult> {
  const {
    model,
    question,
    toolkit = createToolkit(),
    maxSteps = MAX_STEPS,
    today = new Date(),
    onEvent,
    signal,
  } = options;

  if (maxSteps < 2) {
    throw new Error("maxSteps must be at least 2: one to look up, one to answer.");
  }

  const messages: ChatMessage[] = options.messages
    ? [...options.messages]
    : [{ role: "user", content: requireQuestion(question) }];

  const tokens: TokenUsage = {};
  let text = "";
  let reason: AgentResult["reason"] = "steps-exhausted";
  let step = 0;

  for (; step < maxSteps; step += 1) {
    const isLast = step === maxSteps - 1;
    const toolChoice = phaseFor(step, maxSteps);

    onEvent?.({ type: "step-start", step, toolChoice });

    const response: ChatResponse = await model.complete({
      system: isLast ? lastStepPrompt(today) : systemPrompt(today),
      messages,
      tools: toolkit.definitions,
      toolChoice,
      signal,
    });

    accumulate(tokens, response.usage);
    if (response.text) text = response.text;

    onEvent?.({
      type: "step-end",
      step,
      text: response.text,
      usage: response.usage,
    });

    // No tool calls means the model answered. On the last step that is the only
    // legal outcome, because tools were denied.
    if (response.toolCalls.length === 0) {
      reason = "answered";
      messages.push({ role: "assistant", content: response.text, toolCalls: [] });
      break;
    }

    messages.push({
      role: "assistant",
      content: response.text,
      toolCalls: response.toolCalls,
    });

    /*
     * Run them together.
     *
     * The tool description tells the model that independent lookups in one step
     * are cheaper than lookups spread across steps, so it does issue several at
     * once — and running those serially would throw away the only reason it was
     * asked to batch them. Adapters that must not overlap serialise themselves;
     * GDELT does this with the `spaced` gate in core/http.ts.
     */
    const results = await Promise.all(
      response.toolCalls.map(async (call): Promise<ToolResult> => {
        onEvent?.({ type: "tool-call", step, call });
        const startedAt = Date.now();

        // dispatch never throws — it converts failures into an object the model
        // can read and act on. A rejection here would be a bug in the toolkit,
        // not a failed API, so it is not caught.
        const output = await toolkit.dispatch(call.name, call.arguments);
        const isError = isErrorPayload(output);
        const result: ToolResult = {
          callId: call.id,
          name: call.name,
          output,
          isError,
        };

        onEvent?.({
          type: "tool-result",
          step,
          result,
          elapsedMs: Date.now() - startedAt,
        });
        return result;
      }),
    );

    messages.push({ role: "tool", results });
  }

  onEvent?.({ type: "done", reason });

  return {
    text,
    messages,
    steps: Math.min(step + 1, maxSteps),
    usage: toolkit.usage(),
    tokens,
    reason,
  };
}

/*
 * Tools are forced at the start of the turn and denied at the end of it.
 *
 * FORCED (steps 0 and 1). Small models emit reliable tool calls only when the
 * choice is forced; unforced, they answer from memory. Measured on an 8b model
 * asked how many US banks failed in a period: it called find_apis, received
 * FDIC as a Tier A match, then never called it — and answered "0 bank failures"
 * with an invented URL, an invented query date, and the sentence "This answer
 * rests on Tier A evidence". The real figure was four. A fabricated citation is
 * worse than no answer, and this is the documented mitigation. Step 0 forces
 * the lookup; step 1 forces the model to actually call something with what the
 * lookup returned, which is the step it skipped. From step 2 it is free.
 *
 * DENIED (last step). Otherwise the turn can end on a tool result with no
 * answer after it. See `lastStepPrompt`: saying WHY the budget is gone matters
 * as much as the denial, or the model signs off mid-thought with "let me
 * check".
 */
function phaseFor(step: number, maxSteps: number): ToolChoice {
  if (step === maxSteps - 1) return "none";
  if (step <= 1) return "required";
  return "auto";
}

/**
 * Did this tool result represent a failure?
 *
 * Only used to set a flag for the provider and for your logs — the payload goes
 * to the model either way, because the error text is what tells it what to do
 * next. Every failure path in the toolkit sets an `error` key.
 */
function isErrorPayload(output: unknown): boolean {
  return (
    typeof output === "object" &&
    output !== null &&
    "error" in (output as Record<string, unknown>)
  );
}

function accumulate(into: TokenUsage, from: TokenUsage | undefined): void {
  if (!from) return;
  if (from.inputTokens !== undefined) {
    into.inputTokens = (into.inputTokens ?? 0) + from.inputTokens;
  }
  if (from.outputTokens !== undefined) {
    into.outputTokens = (into.outputTokens ?? 0) + from.outputTokens;
  }
  if (from.totalTokens !== undefined) {
    into.totalTokens = (into.totalTokens ?? 0) + from.totalTokens;
  }
}

function requireQuestion(question: string | undefined): string {
  if (!question || !question.trim()) {
    throw new Error("Pass either `question` or a non-empty `messages` array.");
  }
  return question;
}
