import type {
  ChatMessage,
  ChatModel,
  ChatRequest,
  ChatResponse,
  ToolCall,
} from "../agent/model.ts";

/**
 * A `ChatModel` for any OpenAI-compatible `/chat/completions` endpoint.
 *
 * Which is most of them: OpenAI itself, OpenRouter, Together, Groq, DeepSeek,
 * Mistral, vLLM, Ollama, LM Studio, and the SoCLaaS gateway this agent was
 * built against. Plain `fetch`, so the package needs no provider SDK.
 *
 * If your provider is not OpenAI-shaped, do not bend it to fit — implement
 * `ChatModel` directly. It is one method. The README has a Claude example
 * using the official Anthropic SDK.
 */

export type OpenAICompatibleOptions = {
  /** e.g. "https://api.openai.com/v1". A trailing slash is fine. */
  baseUrl: string;
  apiKey: string;
  model: string;
  /**
   * Whole-request ceiling. Generous by default: a research step can sit behind
   * a slow gateway, and the loop already bounds total work by step count.
   */
  timeoutMs?: number;
  /** Merged into every request body — temperature, max_tokens, and so on. */
  extraBody?: Record<string, unknown>;
  headers?: Record<string, string>;
};

export function openAICompatible(options: OpenAICompatibleOptions): ChatModel {
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const timeoutMs = options.timeoutMs ?? 120_000;

  return {
    id: options.model,

    async complete(request: ChatRequest): Promise<ChatResponse> {
      const body = {
        model: options.model,
        messages: [
          { role: "system", content: request.system },
          ...request.messages.flatMap(toWireMessages),
        ],
        tools: request.tools.map((definition) => ({
          type: "function",
          function: {
            name: definition.name,
            description: definition.description,
            parameters: definition.parameters,
          },
        })),
        // "auto" | "required" | "none" are the OpenAI spellings, so this maps
        // straight through. Gateways that do not implement "required" tend to
        // ignore it rather than error — if the first two steps are answering
        // from memory instead of calling a tool, that is the cause.
        tool_choice: request.toolChoice,
        stream: false,
        ...options.extraBody,
      };

      const response = await fetch(`${baseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${options.apiKey}`,
          ...options.headers,
        },
        body: JSON.stringify(body),
        signal: mergeSignals(request.signal, timeoutMs),
      });

      const raw = await response.text();

      if (!response.ok) {
        // Surface the provider's own message. Quota and model-permission errors
        // arrive here and are exactly the ones worth reading verbatim.
        throw new Error(
          `${new URL(baseUrl).host} returned ${response.status}: ${raw.slice(0, 500)}`,
        );
      }

      let parsed: WireResponse;
      try {
        parsed = JSON.parse(raw) as WireResponse;
      } catch {
        throw new Error(
          `${new URL(baseUrl).host} returned a non-JSON body: ${raw.slice(0, 200)}`,
        );
      }

      const choice = parsed.choices?.[0];
      if (!choice) {
        throw new Error(`No choices in response: ${raw.slice(0, 200)}`);
      }

      return {
        text: choice.message?.content ?? "",
        toolCalls: (choice.message?.tool_calls ?? []).map(toToolCall),
        finishReason: choice.finish_reason,
        usage: {
          inputTokens: parsed.usage?.prompt_tokens,
          outputTokens: parsed.usage?.completion_tokens,
          totalTokens: parsed.usage?.total_tokens,
        },
      };
    },
  };
}

type WireResponse = {
  choices?: Array<{
    finish_reason?: string;
    message?: {
      content?: string | null;
      tool_calls?: Array<{
        id?: string;
        function?: { name?: string; arguments?: string };
      }>;
    };
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
};

/**
 * One neutral message becomes one or more wire messages.
 *
 * The asymmetry is tool results: the neutral form groups a step's results
 * together, because that is how the model produced the calls, but the OpenAI
 * wire format wants one `role: "tool"` message per call.
 */
function toWireMessages(message: ChatMessage): Array<Record<string, unknown>> {
  if (message.role === "user") {
    return [{ role: "user", content: message.content }];
  }

  if (message.role === "assistant") {
    const assistant: Record<string, unknown> = {
      role: "assistant",
      content: message.content || null,
    };
    if (message.toolCalls.length > 0) {
      assistant.tool_calls = message.toolCalls.map((call) => ({
        id: call.id,
        type: "function",
        function: {
          name: call.name,
          arguments: JSON.stringify(call.arguments),
        },
      }));
    }
    return [assistant];
  }

  return message.results.map((result) => ({
    role: "tool",
    tool_call_id: result.callId,
    content: JSON.stringify(result.output),
  }));
}

function toToolCall(
  call: { id?: string; function?: { name?: string; arguments?: string } },
  index: number,
): ToolCall {
  return {
    // Some gateways omit the id. Synthesising one keeps the assistant message
    // and its tool results matched up, which is all the id is for.
    id: call.id ?? `call_${index}`,
    name: call.function?.name ?? "",
    arguments: parseArguments(call.function?.arguments),
  };
}

/**
 * Tool arguments arrive as a JSON string.
 *
 * A model can emit one that does not parse. Passing the raw text through rather
 * than throwing lets the toolkit's schema check reject it and hand the model a
 * readable validation error, which it can correct on the next step — the same
 * recovery path a wrong parameter takes.
 */
function parseArguments(text: string | undefined): unknown {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function mergeSignals(
  outer: AbortSignal | undefined,
  timeoutMs: number,
): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return outer ? AbortSignal.any([outer, timeout]) : timeout;
}
