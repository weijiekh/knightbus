import { test } from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "./[[path]].js";

const env = { API_ORIGIN: "https://backend.example" };

test("forwards path and query to API_ORIGIN", async (t) => {
  t.mock.method(globalThis, "fetch", async (req) => {
    assert.equal(req.url, "https://backend.example/v1/stops/nearby?lat=3.1&lon=101.6");
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  });
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/stops/nearby?lat=3.1&lon=101.6"), env });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "[]");
});

test("returns 502 JSON when backend unreachable", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("network"); });
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/health"), env });
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { detail: "backend unreachable" });
});

test("rejects non-GET methods", async () => {
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/health", { method: "POST" }), env });
  assert.equal(res.status, 405);
});
