/**
 * Body cap and disk cache in src/core/http.ts.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MAX_BODY_BYTES, TooLargeError, getText, getTextCached, readCapped } from "../src/core/http.ts";

test("a body over the cap is an error, never a silently cut text", async () => {
  const big = new Response("x".repeat(1024), { headers: {} });
  await assert.rejects(readCapped(big, { context: "test", maxBytes: 100 }), TooLargeError);
});

test("a declared Content-Length over the cap fails before reading", async () => {
  const declared = new Response("small", { headers: { "content-length": String(MAX_BODY_BYTES + 1) } });
  await assert.rejects(readCapped(declared, { context: "test" }), TooLargeError);
});

test("a body under the cap comes back whole", async () => {
  const text = await readCapped(new Response("hello"), { context: "test", maxBytes: 100 });
  assert.equal(text, "hello");
});

test("getTextCached: one download shared by parallel calls, then served from disk", async () => {
  process.env.APIAGENT_CACHE_DIR = mkdtempSync(join(tmpdir(), "apiagent-cache-test-"));
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    return new Response("payload");
  }) as typeof fetch;
  try {
    const url = "https://example.invalid/file.txt";
    const [a, b] = await Promise.all([
      getTextCached(url, { context: "test", maxAgeMs: 60_000 }),
      getTextCached(url, { context: "test", maxAgeMs: 60_000 }),
    ]);
    assert.equal(a, "payload");
    assert.equal(b, "payload");
    assert.equal(calls, 1);
    assert.equal(readdirSync(process.env.APIAGENT_CACHE_DIR!).length, 1);
    assert.equal(await getTextCached(url, { context: "test", maxAgeMs: 60_000 }), "payload");
    assert.equal(calls, 1);                                 // disk hit, no new download
    await getTextCached(url, { context: "test", maxAgeMs: 0 });
    assert.equal(calls, 2);                                 // expired -> fresh download
    await getText(url, { context: "test" });
    assert.equal(calls, 3);                                 // plain getText never caches
  } finally {
    globalThis.fetch = realFetch;
  }
});
