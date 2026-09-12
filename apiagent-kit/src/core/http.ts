/**
 * Shared HTTP for every adapter.
 *
 * Two rules hold this file together, and both come from a documented failure.
 *
 * 1. A 200 is not a success. `api-registry-crossref.md` §3 records a scrape
 *    that returned firecrawl status `ok` with a 21-character body reading
 *    "Status: 403 Forbidden"; the audit logged `scraped-failed=0` and no retry
 *    fired. Every response here is checked for SHAPE, not just status. GDELT
 *    still does exactly this — an HTML error page under a 200.
 *
 * 2. Fail loudly. A shape mismatch throws `ShapeError` carrying the first 200
 *    characters of what actually arrived, so the tool layer can hand the model
 *    a real explanation instead of an empty result set.
 *
 * NOTE ON IMPORTS: every file in this package uses explicit `.ts` extensions
 * and no path aliases, so the whole thing runs under plain Node with no
 * TypeScript runner installed. Keep that property — it is what makes this a
 * drop-in folder rather than something that needs a build step.
 *
 * NOTE ON SYNTAX: keep this subtree pure-erasure TypeScript — no enums, no
 * constructor parameter properties, no namespaces, no decorators. Node's
 * type-stripping mode rejects anything that is not erasable, and that mode is
 * the whole reason there is no build step.
 */

/**
 * Per-request ceiling. The gateway allows 300s for the WHOLE turn, and a turn
 * can hold six steps with several parallel calls in each, so no single API is
 * allowed to eat a meaningful share of it.
 *
 * Raised from 10s after a measured failure. Asked for US CPI, the agent called
 * FRED four times and every one timed out, so it reported that it could not
 * answer — while curl fetched the same URL in 0.67s. Timing the endpoint from
 * Node with identical headers gave 1.4s, 2.0s, 5.1s, 11.1s, 12.9s and 13.1s on
 * consecutive attempts: not a cold start, not a header problem, just wide
 * variance through Akamai. A 10s ceiling sat in the middle of that spread, so
 * FRED failed perhaps half the time, at random.
 *
 * 15s covers the bulk of that distribution while still bounding a dead host.
 * Adapters measured to need more say so themselves via `timeoutMs`.
 */
export const REQUEST_TIMEOUT_MS = 15_000;

/**
 * Chars of any one adapter result the model is allowed to see. The same number
 * the chat project settled on for a fetched page (`lib/web-fetch.ts`).
 */
export const MAX_RESULT_CHARS = 8_000;

/** Chars across every adapter call in one turn. Three full results. */
export const TURN_CHAR_BUDGET = MAX_RESULT_CHARS * 3;

/**
 * Rows any one adapter returns. Enough to see a pattern, small enough that six
 * parallel calls cannot blow the context of a 27b model.
 */
export const MAX_ROWS = 25;

/**
 * Response body bytes read before giving up. Guards against an endpoint that
 * decides to stream a few hundred megabytes at us — CISA KEV is already ~2 MB.
 */
const MAX_BODY_BYTES = 8 * 1024 * 1024;

/**
 * The response arrived, but it is not what this endpoint is documented to
 * return. Distinct from `HttpError` because the two want different handling: an
 * HTTP error is usually transient, a shape error means the endpoint changed and
 * the adapter is now wrong.
 */
export class ShapeError extends Error {
  /* Fields are declared and assigned rather than written as constructor
   * parameter properties: `smoke.ts` runs under Node's strip-only TypeScript
   * mode, which rejects any syntax that is not pure erasure. Keeping this
   * subtree erasable is what lets the smoke test run with no toolchain. */
  readonly context: string;
  readonly expected: string;
  readonly received: string;

  constructor(context: string, expected: string, received: string) {
    super(`${context}: expected ${expected}, got ${received}`);
    this.name = "ShapeError";
    this.context = context;
    this.expected = expected;
    this.received = received;
  }
}

/** Non-2xx. Carries the status so an adapter can treat 404 as "no rows". */
export class HttpError extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: string;

  constructor(status: number, url: string, body: string) {
    super(`${new URL(url).host} returned ${status}`);
    this.name = "HttpError";
    this.status = status;
    this.url = url;
    this.body = body;
  }
}

/**
 * What arrived, short enough to put in an error message.
 *
 * The whole point of the §3 failure was that nobody looked at the body. A shape
 * error that does not show the body reproduces the bug in a new place.
 */
function preview(value: unknown, limit = 200): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text === undefined) return String(value);
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/**
 * Throw unless the body looks like what the endpoint promises.
 *
 * `expected` is prose, not a schema: it lands in an error message a human reads
 * when an endpoint changes under us, and "an object with a studies array" beats
 * any serialised type.
 */
export function assertShape(
  value: unknown,
  ok: (value: unknown) => boolean,
  context: string,
  expected: string,
): void {
  if (!ok(value)) throw new ShapeError(context, expected, preview(value));
}

/**
 * Descriptive User-Agent.
 *
 * Several of these APIs — SEC most strictly — refuse or throttle requests that
 * do not identify themselves. SEC asks for a contact address; that is read from
 * `SEC_EDGAR_CONTACT` rather than baked in, because it is a personal detail
 * being sent to a third party and that should be an explicit opt-in.
 */
/*
 * The conventional declared-crawler format, `Product/version (+url)`, and the
 * shape matters more than it looks.
 *
 * MEASURED 2026-09-09. From this network every reasonable UA reaches
 * api.stlouisfed.org in well under a second — 15 consecutive calls with the old
 * string returned in 0.32-1.30s, none slow. From a Vercel function the same
 * endpoint behaved completely differently depending on the string:
 *
 *   SoCLaaS-Experiments/1.0 (+https://...)     fast, every time
 *   SOCLAAS-Experiments/apiagent (research agent)   30s timeout, 6 of 6
 *
 * Everything else was held constant, and the network itself was cleared: a
 * cache-busted origin fetch of real data from the same Vercel function returned
 * promptly. Akamai fronts FRED and scores requests on User-Agent together with
 * source-IP reputation; an uncategorised string from a datacenter range gets
 * tarpitted rather than refused, which is why it surfaced as a timeout and not
 * a 403. The same host tarpits a BROWSER UA from here for 19-40s, so the
 * classifier is demonstrably doing this.
 *
 * Keep the `+url` form. It is what bot-management systems recognise as a
 * declared crawler, and it is the difference between working and not.
 *
 * Both halves are configurable so this package can identify itself as YOUR
 * project rather than the one it was extracted from. Change them via
 * `APIAGENT_UA_PRODUCT` and `APIAGENT_UA_URL`, and keep the shapes:
 * `Name/version` and a real https URL that describes the crawler. A blank or
 * jokey value re-creates the tarpit described above.
 */
const PRODUCT = process.env.APIAGENT_UA_PRODUCT?.trim() || "SoCLaaS-Experiments/1.0";
const SITE = process.env.APIAGENT_UA_URL?.trim() || "https://soclaas-experiments.vercel.app";

export function userAgent(): string {
  const contact = process.env.SEC_EDGAR_CONTACT?.trim();
  const inside = contact ? `+${SITE}; ${contact}` : `+${SITE}`;
  return `${PRODUCT} (${inside})`;
}

type RequestOptions = {
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Named in errors, so the model is told which API failed and how. */
  context: string;
  /**
   * Override the default ceiling. Only for endpoints measured to need it —
   * GDELT takes upwards of twelve seconds even to REFUSE a request, so a
   * ten-second cap turns a rate-limit message we could act on into an
   * unexplained timeout.
   */
  timeoutMs?: number;
};

/**
 * Timeout that respects an outer signal.
 *
 * The turn can be aborted (the reader pressed Stop) while an adapter is also
 * running its own clock, and whichever fires first should win.
 */
function withTimeout(
  signal: AbortSignal | undefined,
  timeoutMs = REQUEST_TIMEOUT_MS,
): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

/**
 * Minimum spacing between calls sharing a key, enforced in-process.
 *
 * GDELT publishes a hard limit of one request every five seconds and answers
 * violations with a 429. That collides directly with the design's advice to fan
 * `call_api` out in parallel — six simultaneous GDELT calls are five refusals
 * and one answer. Serialising them here is invisible to the model and costs
 * only wall clock, which the turn has more of than it has retries.
 *
 * Per-instance, like the caches elsewhere in this project: on a serverless
 * platform that means per-process and gone on a cold start. It cannot enforce a
 * global limit across instances, and is not trying to — it removes the
 * self-inflicted case where one turn races itself.
 */
const gates = new Map<string, Promise<void>>();

export function spaced<T>(
  key: string,
  minIntervalMs: number,
  work: () => Promise<T>,
): Promise<T> {
  const previous = gates.get(key) ?? Promise.resolve();
  const started = previous.then(work);

  // The chain advances on settle, not on success: a failed call still consumed
  // the endpoint's allowance, so the next one must still wait.
  gates.set(
    key,
    started.then(
      () => new Promise((resolve) => setTimeout(resolve, minIntervalMs)),
      () => new Promise((resolve) => setTimeout(resolve, minIntervalMs)),
    ),
  );

  return started;
}

async function readCapped(response: Response): Promise<string> {
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  const slice =
    bytes.length > MAX_BODY_BYTES ? bytes.subarray(0, MAX_BODY_BYTES) : bytes;
  return new TextDecoder("utf-8").decode(slice);
}

/**
 * Raw text, status-checked but not shape-checked. Callers that want a shape
 * check call `assertShape` on the parsed form themselves.
 */
export async function getText(url: string, options: RequestOptions): Promise<string> {
  const response = await fetch(url, {
    headers: { "User-Agent": userAgent(), ...options.headers },
    signal: withTimeout(options.signal, options.timeoutMs),
    redirect: "follow",
  });

  const body = await readCapped(response);
  if (!response.ok) throw new HttpError(response.status, url, preview(body));
  return body;
}

/**
 * JSON with a mandatory shape check.
 *
 * `expect` is not optional. An adapter that cannot say what a good response
 * looks like has not been verified against the live endpoint, and this file
 * exists to stop that shipping.
 */
export async function getJson<T>(
  url: string,
  options: RequestOptions & {
    expect: (value: unknown) => boolean;
    expected: string;
  },
): Promise<T> {
  const text = await getText(url, options);

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    // The §3 failure exactly: a 200 carrying an HTML error page. Say so, rather
    // than letting a parse error surface as an unexplained crash.
    throw new ShapeError(options.context, "JSON", preview(text));
  }

  assertShape(parsed, options.expect, options.context, options.expected);
  return parsed as T;
}

/**
 * Trim to `MAX_ROWS`, reporting whether anything was dropped so the adapter can
 * tell the model its answer is partial rather than complete.
 */
export function capRows<T>(
  rows: T[],
  max = MAX_ROWS,
): { rows: T[]; truncated: boolean } {
  return rows.length > max
    ? { rows: rows.slice(0, max), truncated: true }
    : { rows, truncated: false };
}

/** Field-level cap. Abstracts and article bodies are the usual offenders. */
export function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** Shorthand for the commonest shape check: a plain object, not an array. */
export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Minimal RFC 4180 CSV. Iowa Environmental Mesonet returns CSV and nothing else
 * here does, so this is deliberately small rather than a dependency: quoted
 * fields, doubled quotes inside them, CRLF or LF.
 */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];

    if (quoted) {
      if (char !== DQUOTE) {
        field += char;
      } else if (text[i + 1] === DQUOTE) {
        field += DQUOTE;
        i += 1;
      } else {
        quoted = false;
      }
      continue;
    }

    if (char === DQUOTE) quoted = true;
    else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") field += char;
  }

  if (field !== "" || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

const DQUOTE = String.fromCharCode(34);

/** CSV rows as objects keyed by the header line. */
export function csvToObjects(text: string): Array<Record<string, string>> {
  const rows = parseCsv(text).filter((row) => row.some((cell) => cell.trim() !== ""));
  const header = rows.shift();
  if (!header) return [];
  return rows.map((row) =>
    Object.fromEntries(
      header.map((key, index) => [key.trim(), (row[index] ?? "").trim()]),
    ),
  );
}

/**
 * ISO instant for the provenance log. Every result carries one — the Tier C
 * policy in §3 requires the retrieval URL AND timestamp, not just the URL.
 */
export function now(): string {
  return new Date().toISOString();
}
