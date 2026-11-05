import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
function source(start, end) {
  const offset = html.indexOf(start);
  assert.notEqual(offset, -1, `Missing source boundary: ${start}`);
  const finish = html.indexOf(end, offset);
  assert.notEqual(finish, -1, `Missing source boundary: ${end}`);
  return html.slice(offset, finish);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fetchFixture() {
  const context = vm.createContext({
    apiFetch: async () => [],
    fetchEtas: async id => ({ stop_id: id, items: [] }),
    rememberStopFromEtas() {},
  });
  vm.runInContext(source("async function fetchEtasMany(stopIds)", "function setOffline(on)"), context);
  return context;
}

test("fetchEtasMany batches sequentially in chunks of at most ten and preserves order", async () => {
  const context = fetchFixture();
  const sizes = [];
  context.apiFetch = async path => {
    const ids = new URL(`https://example.test${path}`).searchParams.get("stop_ids").split(",");
    sizes.push(ids.length);
    return ids.reverse().map(stop_id => ({ stop_id }));
  };
  const ids = Array.from({ length: 23 }, (_, i) => `s${i}`);
  const results = await vm.runInContext("fetchEtasMany(ids)", Object.assign(context, { ids }));
  assert.deepEqual(sizes, [10, 10, 3]);
  assert.deepEqual(Array.from(results, item => item.stop_id), ids);
});

test("fetchEtasMany falls back within bounded chunks when batch endpoint is unsupported", async () => {
  const context = fetchFixture();
  const batchSizes = [];
  let perStop = 0;
  context.apiFetch = async path => {
    const ids = new URL(`https://example.test${path}`).searchParams.get("stop_ids").split(",");
    batchSizes.push(ids.length);
    const err = new Error("unsupported"); err.status = 422; throw err;
  };
  context.fetchEtas = async id => { perStop++; return { stop_id: id, items: [] }; };
  const ids = Array.from({ length: 12 }, (_, i) => `s${i}`);
  const results = await vm.runInContext("fetchEtasMany(ids)", Object.assign(context, { ids }));
  assert.deepEqual(batchSizes, [10, 2]);
  assert.equal(perStop, 12);
  assert.deepEqual(Array.from(results, item => item.stop_id), ids);
});

function savedFixture() {
  const context = vm.createContext({
    S: { saved: ["a", "b"], view: "saved", lastLoadAt: 0 },
    fetchEtasMany: async () => [],
    drawSaved() {},
    Date,
  });
  vm.runInContext(source("const savedState = { etas: {} };", "let savedReorder = false"), context);
  return context;
}

test("loadSaved maps snapshotted IDs, preserves failed entries, and replaces successful empty results", async () => {
  const context = savedFixture();
  vm.runInContext('savedState.etas.a = { marker: "old-a", generated_at: "2026-01-01T00:00:00Z" }', context);
  vm.runInContext('savedState.etas.b = { marker: "old-b" }', context);
  context.fetchEtasMany = async ids => {
    assert.deepEqual(Array.from(ids), ["a", "b"]);
    context.S.saved.reverse();
    return [{ marker: "new-a" }, { stop_id: "b", items: [] }];
  };
  await vm.runInContext("loadSaved()", context);
  assert.equal(vm.runInContext("savedState.etas.a.marker", context), "new-a");
  assert.equal(vm.runInContext("savedState.etas.b.items.length", context), 0);
  assert.deepEqual(Array.from(context.S.saved), ["b", "a"]);
});

test("loadSaved preserves the prior value when a saved stop refresh fails", async () => {
  const context = savedFixture();
  vm.runInContext('savedState.etas.a = { marker: "old-a", generated_at: "2026-01-01T00:00:00Z" }', context);
  vm.runInContext('savedState.etas.b = { marker: "old-b" }', context);
  context.fetchEtasMany = async () => [{ marker: "new-a" }, null];
  await vm.runInContext("loadSaved()", context);
  assert.equal(vm.runInContext("savedState.etas.a.marker", context), "new-a");
  assert.equal(vm.runInContext("savedState.etas.b.marker", context), "old-b");
});

test("an older overlapping saved load cannot overwrite a newer successful result", async () => {
  const context = savedFixture();
  const oldRequest = deferred();
  let calls = 0;
  context.fetchEtasMany = () => ++calls === 1 ? oldRequest.promise : Promise.resolve([{ marker: "new-a" }, { marker: "new-b" }]);
  const oldLoad = vm.runInContext("loadSaved()", context);
  await vm.runInContext("loadSaved()", context);
  oldRequest.resolve([{ marker: "old-a" }, { marker: "old-b" }]);
  await oldLoad;
  assert.equal(vm.runInContext("savedState.etas.a.marker", context), "new-a");
  assert.equal(vm.runInContext("savedState.etas.b.marker", context), "new-b");
});

function nearbyFixture() {
  const clock = { now: 1_000_000 };
  class FakeDate extends Date { static now() { return clock.now; } }
  const context = vm.createContext({
    S: { loc: { lat: 3.1, lon: 101.6 }, nearby: null, nearbyAt: 0, view: "home" },
    NEARBY_RADIUS_M: 1000, NEARBY_LIMIT: 6,
    apiFetch: async () => [], fetchEtasMany: async () => [],
    rememberStop() {}, renderHomeBody() {}, drawMap() {}, map: {}, Date: FakeDate,
    advanceClock: ms => { clock.now += ms; },
  });
  vm.runInContext(source("let nearbySeq = 0;", "// ══════════ MAP ══════════"), context);
  return context;
}

test("nearby stop lookup is cached while ETA refresh still runs", async () => {
  const context = nearbyFixture();
  let lookupCalls = 0, etaCalls = 0;
  context.apiFetch = async () => { lookupCalls++; return [{ stop_id: "s1", name: "Stop" }]; };
  context.fetchEtasMany = async () => { etaCalls++; return []; };
  await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(lookupCalls, 1);
  context.S.nearby = [{ stop: { stop_id: "s1" }, eta: { marker: "old", generated_at: "2026-01-01T00:00:00Z" } }];
  context.S.loc = { lat: 3.1, lon: 101.6 };
  context.apiFetch = async () => { lookupCalls++; return [{ stop_id: "s1", name: "Stop" }]; };
  context.fetchEtasMany = async () => { etaCalls++; return [null]; };
  await vm.runInContext("loadNearby(true)", context);
  assert.equal(lookupCalls, 1);
  assert.equal(etaCalls, 1);
  assert.equal(context.S.nearby[0].eta.marker, "old");
  assert.equal(context.S.nearbyAt, 0);
});

test("nearby stop cache expires after five minutes", async () => {
  const context = nearbyFixture();
  let calls = 0;
  context.apiFetch = async () => [{ stop_id: `s${++calls}`, name: "Stop" }];
  const first = await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  context.advanceClock(5 * 60 * 1000 - 1);
  const cached = await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(calls, 1);
  assert.equal(cached[0].stop_id, first[0].stop_id);
  context.advanceClock(2);
  const refreshed = await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(calls, 2);
  assert.equal(refreshed[0].stop_id, "s2");
});

test("an empty nearby result is cached as a successful lookup", async () => {
  const context = nearbyFixture();
  let calls = 0;
  context.apiFetch = async () => { calls++; return []; };
  await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  const result = await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(result.length, 0);
  assert.equal(calls, 1);
});

test("failed and obsolete nearby discovery requests are not cached; same-key requests are deduplicated", async () => {
  const context = nearbyFixture();
  const first = deferred();
  let calls = 0;
  context.apiFetch = () => { calls++; return calls === 1 ? Promise.reject(new Error("offline")) : first.promise; };
  const failed = vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq).catch(() => null)", context);
  await failed;
  const stale = vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  const duplicate = vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(calls, 2);
  first.resolve([{ stop_id: "old-location" }]);
  await Promise.all([stale, duplicate]);
  assert.equal(vm.runInContext("nearbyLookupCache.size", context), 1);
  context.S.loc = { lat: 3.2, lon: 101.7 };
  context.apiFetch = async () => { calls++; return [{ stop_id: "retry" }]; };
  await vm.runInContext("nearbyStopsFor(S.loc, ++nearbySeq)", context);
  assert.equal(calls, 3);
});

test("a superseded location request cannot replace the current nearby state", async () => {
  const context = nearbyFixture();
  const oldStops = deferred();
  let call = 0;
  context.apiFetch = async () => ++call === 1 ? oldStops.promise : [{ stop_id: "current", name: "Current" }];
  context.fetchEtasMany = async () => [{ marker: "fresh" }];
  const oldLoad = vm.runInContext("loadNearby(true)", context);
  context.S.loc = { lat: 3.2, lon: 101.7 };
  await vm.runInContext("loadNearby(true)", context);
  oldStops.resolve([{ stop_id: "old", name: "Old" }]);
  await oldLoad;
  assert.equal(context.S.nearby[0].stop.stop_id, "current");
  assert.equal(vm.runInContext("nearbyLookupCache.size", context), 1);
});

test("a location change while ETA refresh is pending cannot update nearby state", async () => {
  const context = nearbyFixture();
  const etaRequest = deferred();
  let etaStarted;
  const started = new Promise(resolve => { etaStarted = resolve; });
  context.apiFetch = async () => [{ stop_id: "old-location", name: "Old" }];
  context.fetchEtasMany = () => { etaStarted(); return etaRequest.promise; };
  const load = vm.runInContext("loadNearby(true)", context);
  await started;
  context.S.loc = { lat: 3.2, lon: 101.7 };
  etaRequest.resolve([{ marker: "fresh" }]);
  await load;
  assert.equal(context.S.nearby, null);
  assert.equal(context.S.nearbyAt, 0);
});
