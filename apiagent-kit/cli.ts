/**
 * A one-shot JSON bridge over the registry, for callers that are not an LLM.
 *
 *   echo '{"op":"find","need":"US unemployment rate"}' | node cli.ts
 *   echo '{"op":"call","api":"fred","params":{"series":"UNRATE"}}' | node cli.ts
 *   echo '{"op":"list"}' | node cli.ts
 *
 * WHY THIS EXISTS. The Python forecasting bot in the parent directory cannot
 * import TypeScript, so every call crosses a process boundary. One JSON object
 * in on stdin, one JSON object out on stdout.
 *
 * WHY THE REGISTRY AND NOT THE TOOLKIT. `createToolkit()` is shaped for a
 * model: it caps a turn at TURN_CHAR_BUDGET, wraps rows in a prompt-injection
 * warning, and converts failures into prose advice about what the model should
 * try next. All three are right for an agent loop and wrong for a program,
 * which wants the rows intact and the failure as data. `getAdapter().run()` is
 * the same retrieval with none of the framing. The one thing worth keeping
 * from the toolkit layer is its handling of bad parameters, so that is
 * reimplemented here: a schema failure comes back with `paramsHelp` attached
 * rather than as a stack trace.
 *
 * CONTRACT. Always exits 0 and always prints exactly one JSON object, so the
 * caller never has to parse stderr or interpret an exit code. Failures are
 * `{"ok": false, "error": ...}`. Anything on stderr is diagnostics.
 *
 * NOTHING HERE CALLS process.exit(). An earlier version did, and on Windows it
 * aborted the process outright -- `Assertion failed: !(handle->flags &
 * UV_HANDLE_CLOSING)` -- when an async adapter call was still unwinding while
 * stdin's stream handle was closing. It survived the two synchronous ops and
 * died on the one that does I/O, which is the worst possible failure shape.
 * So: every path RETURNS a payload, the single write happens at top level, and
 * Node exits on its own once the event loop drains.
 *
 * stdin rather than argv: params carry quotes, braces and non-ASCII, and
 * Windows argv quoting mangles all three.
 */
import { adapters, activeAdapters, find, getAdapter } from "./src/index.ts";
import { HttpError, ShapeError } from "./src/index.ts";
import { createToolkit, lastStepPrompt, systemPrompt } from "./src/index.ts";
import { TURN_CHAR_BUDGET } from "./src/core/http.ts";

/*
 * ONE CREDENTIAL, ONE NAME.
 *
 * The kit calls Metaculus's credential METACULUS_API_TOKEN; the bot has always
 * called the same value METACULUS_TOKEN, and both send it as
 * `Authorization: Token <t>`. Asking for it twice in .env is a way for the two
 * copies to drift, and a half-filled pair fails confusingly: the bot runs but
 * the metaculus adapter silently vanishes from the registry.
 *
 * So the bot's name is authoritative and the kit's name is derived from it.
 * An explicitly-set METACULUS_API_TOKEN still wins, so a different token can
 * be used for the adapter if that is ever wanted.
 *
 * Safe to do here: the adapter reads the variable lazily, inside available()
 * and run(), never at import time.
 */
const metaculusToken = process.env.METACULUS_API_TOKEN?.trim()
  || process.env.METACULUS_TOKEN?.trim();
if (metaculusToken) process.env.METACULUS_API_TOKEN = metaculusToken;

/*
 * Identify this project to the upstream APIs.
 *
 * The kit's built-in defaults name the project it was extracted from, which is
 * wrong on two counts: it misattributes our traffic, and the User-Agent is
 * load-bearing rather than cosmetic -- Akamai (which fronts FRED) scores
 * requests on it and tarpits uncategorised datacenter strings into 30-second
 * timeouts rather than refusing them outright.
 *
 * These belong in code, not in .env: they are a property of the project, not
 * of the machine running it, so there is nothing for a deployment to decide.
 * An explicit env value still wins, which is the escape hatch if one host ever
 * needs to be distinguished from another.
 */
process.env.APIAGENT_UA_PRODUCT ||= "Pirohuni-Forecast-Bot/1.0";
process.env.APIAGENT_UA_URL ||= "https://github.com/Pin-Rui-CS/Pirohuni-Forecast-Bot-Fall";

const DEFAULT_TIMEOUT_MS = 45_000;

type Payload = Record<string, unknown>;

type Request = {
  op?: string;
  need?: string;
  api?: string;
  params?: Record<string, unknown>;
  timeoutMs?: number;
  /** op=spec: ISO date the prompts should treat as today. */
  today?: string;
  /** op=tools: one agent step's tool calls. */
  calls?: Array<{ id?: string; name?: string; args?: unknown }>;
  /** op=tools: data characters already returned earlier in this turn. */
  charsSpent?: number;
};

/*
 * THE AGENT OPS. The bot runs the agent loop in Python, because its model
 * calls must go through its own cost accounting and SoCLaaS streaming. The kit
 * stays the source of the loop's tools and prompts:
 *
 *   spec   -> tool definitions + the system and last-step prompts
 *   tools  -> run one step's tool calls through the toolkit, exactly as
 *             runAgent would, and report the data characters they returned
 *
 * One process per STEP (not per call) so parallel calls in a step share the
 * toolkit, and GDELT's in-process rate gate still spaces them. The turn's data
 * budget spans steps, so the caller carries it in `charsSpent`.
 */
function agentSpec(request: Request): Payload {
  const today = request.today ? new Date(request.today) : new Date();
  if (Number.isNaN(today.getTime())) fail(`spec: invalid today ${request.today}`);
  return {
    ok: true,
    definitions: createToolkit().definitions,
    systemPrompt: systemPrompt(today),
    lastStepPrompt: lastStepPrompt(today),
    turnCharBudget: TURN_CHAR_BUDGET,
  };
}

async function runTools(request: Request): Promise<Payload> {
  const calls = Array.isArray(request.calls) ? request.calls : [];
  const spent = Number(request.charsSpent) || 0;
  const toolkit = createToolkit();

  const results = await Promise.all(calls.map(async (call) => {
    const name = String(call.name ?? "");
    // The toolkit's budget counter starts at zero in a fresh process, so the
    // cross-step check happens here, with the toolkit's own wording.
    if (name === "call_api" && spent >= TURN_CHAR_BUDGET) {
      const api = (call.args as { api?: unknown } | undefined)?.api;
      return { id: call.id, output: {
        api, error: "This turn has used its data budget. Answer with what you have " +
          "already gathered and say what you could not check.",
      } };
    }
    return { id: call.id, output: await toolkit.dispatch(name, call.args) };
  }));

  let charsReturned = 0;
  for (const { output } of results) {
    const rows = (output as { rows?: unknown } | null)?.rows;
    if (Array.isArray(rows)) charsReturned += JSON.stringify(rows).length;
  }
  return { ok: true, results, charsReturned, usage: toolkit.usage() };
}

/** Control-flow carrier for an early return, so `fail` can be used mid-expression. */
class Bail extends Error {
  payload: Payload;
  constructor(payload: Payload) {
    super("bail");
    this.payload = payload;
  }
}

function fail(error: string, extra: Payload = {}): never {
  throw new Bail({ ok: false, error, ...extra });
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

/** Adapter ids this process can reach, and which are gated off. */
function listAdapters(): Payload {
  const active = activeAdapters();
  const activeIds = new Set(active.map((a) => a.id));
  return {
    ok: true,
    adapters: active.map((a) => ({
      id: a.id,
      name: a.name,
      tier: a.tier,
      domain: a.domain,
      answers: a.answers,
      paramsHelp: a.paramsHelp,
    })),
    // An adapter absent from `active` is gated off by a missing credential.
    // Reported so the caller can say WHY a source was unavailable rather than
    // behaving as though it never existed.
    unavailable: adapters.filter((a) => !activeIds.has(a.id)).map((a) => a.id),
  };
}

async function callAdapter(request: Request): Promise<Payload> {
  const id = (request.api ?? "").trim();
  if (!id) fail("call requires `api`");

  // getAdapter searches ACTIVE adapters only, so a credential-gated id is
  // simply not found. Distinguishing the two matters: "unknown api id: fred"
  // sends you hunting for a typo when the real answer is that FRED_API_KEY is
  // unset. Check the full registry before calling it unknown.
  const adapter = getAdapter(id);
  if (!adapter) {
    const known = adapters.find((a) => a.id === id);
    if (known) {
      fail(`api ${id} is gated off: its credential is not set`, {
        api: id, kind: "unavailable", unavailable: true,
      });
    }
    fail(`unknown api id: ${id}`, { available: activeAdapters().map((a) => a.id) });
  }

  const timeoutMs = Number(request.timeoutMs) || DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const result = await adapter.run(request.params ?? {}, controller.signal);
    return {
      ok: true,
      api: id,
      tier: result.tier,
      url: result.url,
      retrievedAt: result.retrievedAt,
      rowCount: result.rows.length,
      truncated: result.truncated ?? false,
      note: result.note ?? "",
      rows: result.rows,
    };
  } catch (err) {
    if (err instanceof Bail) throw err;
    const e = err as Error & { status?: number };
    // Zod rejects bad params before any request is made. That is the one
    // failure the caller can fix itself, so hand back the same paramsHelp
    // prose the model would have seen.
    if (e?.name === "ZodError") {
      let problems: unknown = e.message;
      try {
        problems = JSON.parse(e.message);
      } catch {
        /* keep the raw message */
      }
      fail(`invalid params for ${id}`, {
        api: id, kind: "params", problems, paramsHelp: adapter.paramsHelp,
      });
    }
    if (e instanceof ShapeError) {
      fail(`${id} returned an unexpected shape: ${e.message}`, {
        api: id, kind: "shape", retryable: false,
      });
    }
    if (e instanceof HttpError) {
      fail(`${id} HTTP ${e.status}: ${e.message}`, {
        api: id, kind: "http", status: e.status,
        retryable: e.status >= 500 || e.status === 429,
      });
    }
    fail(`${id} failed: ${e?.name ?? "Error"}: ${e?.message ?? String(err)}`, {
      api: id,
      kind: controller.signal.aborted ? "timeout" : "error",
      retryable: controller.signal.aborted,
    });
  } finally {
    clearTimeout(timer);
  }
}

async function main(): Promise<Payload> {
  const raw = (await readStdin()).trim();
  if (!raw) fail("empty stdin; expected one JSON object");

  let request: Request;
  try {
    request = JSON.parse(raw) as Request;
  } catch (err) {
    fail(`stdin is not valid JSON: ${(err as Error).message}`);
  }

  switch (request.op ?? "") {
    case "list":
      return listAdapters();
    case "find": {
      const need = (request.need ?? "").trim();
      if (!need) fail("find requires a non-empty `need`");
      // Deterministic keyword/stem match. No model, no network, no cost.
      return { ok: true, need, candidates: find(need) };
    }
    case "call":
      return await callAdapter(request);
    case "spec":
      return agentSpec(request);
    case "tools":
      return await runTools(request);
    default:
      fail(`unknown op: ${request.op || "(missing)"}`, {
        ops: ["list", "find", "call", "spec", "tools"],
      });
  }
}

const payload: Payload = await main().catch((err: unknown) =>
  err instanceof Bail
    ? err.payload
    : { ok: false, error: `unhandled: ${(err as Error)?.message ?? String(err)}` },
);
process.stdout.write(JSON.stringify(payload));
