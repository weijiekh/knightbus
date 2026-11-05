import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
function section(start, end) {
  const from = html.indexOf(start);
  assert.notEqual(from, -1, `Missing source boundary: ${start}`);
  const to = html.indexOf(end, from);
  assert.notEqual(to, -1, `Missing source boundary: ${end}`);
  return html.slice(from, to);
}
function context(now = Date.now()) {
  const fixedDate = class extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  };
  const ctx = vm.createContext({ Date: fixedDate, esc: value => String(value),
    clock: value => `clock:${value}`, clockFmt: { format: value => value.toISOString().slice(11, 16) },
    routeLabel: item => item.route_short_name || "—", titleCase: String,
    routeBadgeLink: () => '<span class="route-badge">190</span>', delayBadge: () => "",
    S: { routeMeta: {} }, checkWalkingGPS() {},
  });
  vm.runInContext(section("function minutesOf(item)", "function klMinutesOfDay"), ctx);
  vm.runInContext(section("const LIVE_EVIDENCE_MAX_AGE_MS", "function routeLabel"), ctx);
  vm.runInContext(section("function upcoming(data)", "// Group by route_short_name"), ctx);
  vm.runInContext(section("function oldestEvidenceTimestamp(data", "// ══════════ NETWORK"), ctx);
  vm.runInContext(section("function arrivalChip(it)", "let nearbySeq"), ctx);
  vm.runInContext(section("function arrivalCard(group)", "\n\n// ══════════"), ctx);
  return ctx;
}

test("arrival countdown uses predicted time and does not freeze on minutes_away", () => {
  const now = Date.parse("2026-11-04T00:00:00Z");
  const ctx = context(now);
  ctx.item = { predicted_arrival_time: "2026-11-04T00:10:00Z", minutes_away: 45 };
  assert.equal(vm.runInContext("minutesOf(item)", ctx), 10);
  ctx.item.predicted_arrival_time = "2026-11-04T00:00:59Z";
  assert.equal(vm.runInContext("minutesOf(item)", ctx), 0);
  ctx.item = { minutes_away: 3 };
  assert.equal(vm.runInContext("minutesOf(item)", ctx), null);
});

test("upcoming removes arrivals that passed since an old generated_at", () => {
  const now = Date.parse("2026-11-04T00:00:00Z");
  const ctx = context(now);
  ctx.data = { generated_at: "2026-11-03T23:55:00Z", items: [
    { predicted_arrival_time: "2026-11-03T23:59:59Z" },
    { predicted_arrival_time: "2026-11-04T00:01:00Z" },
  ] };
  assert.equal(vm.runInContext("upcoming(data).length", ctx), 1);
});

test("realtime labels require a vehicle and recent observation time", () => {
  const now = Date.parse("2026-11-04T00:00:00Z");
  const ctx = context(now);
  ctx.item = { prediction_source: "heuristic", vehicle_id: "bus-1", data_timestamp: "2026-11-03T23:59:00Z" };
  assert.equal(vm.runInContext("arrivalFreshness(item)", ctx), "live");
  ctx.item.data_timestamp = "2026-11-03T23:55:00Z";
  assert.equal(vm.runInContext("arrivalFreshness(item)", ctx), "stale");
  ctx.item.vehicle_id = null;
  assert.equal(vm.runInContext("arrivalFreshness(item)", ctx), "estimated");
  ctx.item.prediction_source = "schedule";
  assert.equal(vm.runInContext("arrivalFreshness(item)", ctx), "timetable");
});

test("older realtime evidence is not hidden by a newer sibling or schedule timestamp", () => {
  const ctx = context();
  ctx.data = { generated_at: "2026-11-04T00:00:00Z", items: [
    { prediction_source: "heuristic", vehicle_id: "old", data_timestamp: "2026-11-03T23:40:00Z" },
    { prediction_source: "heuristic", vehicle_id: "new", data_timestamp: "2026-11-03T23:59:30Z" },
    { prediction_source: "schedule", data_timestamp: "2026-11-04T00:00:00Z" },
  ] };
  assert.equal(vm.runInContext("oldestEvidenceTimestamp(data)", ctx), Date.parse("2026-11-03T23:40:00Z"));
});

test("arrival cards distinguish live, estimated, stale, and timetable entries", () => {
  const ctx = context(Date.parse("2026-11-04T00:00:00Z"));
  const render = item => {
    ctx.group = [{ route_short_name: "190", predicted_arrival_time: "2026-11-04T00:10:00Z", ...item }];
    return vm.runInContext("arrivalCard(group)", ctx);
  };
  const live = render({ prediction_source: "heuristic", vehicle_id: "bus", data_timestamp: "2026-11-03T23:59:30Z" });
  assert.match(live, />LIVE</);
  assert.match(live, /data-freshness="live"/);
  assert.match(live, /data-evidence-at="1793750370000"/);
  assert.match(render({ prediction_source: "heuristic", data_timestamp: "2026-11-03T23:59:30Z" }), />ESTIMATED</);
  assert.match(render({ prediction_source: "heuristic", vehicle_id: "bus", data_timestamp: "2026-11-03T23:55:00Z" }), />STALE</);
  assert.match(render({ prediction_source: "schedule", data_timestamp: "2026-11-04T00:00:00Z" }), />TIMETABLE</);
});

test("visible tick rerenders cached arrivals when a countdown expires", () => {
  const now = Date.parse("2026-11-04T00:00:00Z");
  let tick, rendered = 0;
  const expired = { dataset: { countdown: "2026-11-03T23:59:59Z" }, closest: () => null };
  const ctx = context(now);
  ctx.document = {
    visibilityState: "visible", activeElement: null,
    querySelectorAll: selector => selector === "[data-countdown]" ? [expired] : [],
    getElementById: () => null,
  };
  ctx.S = { view: "stop", lastLoadAt: 0 };
  ctx.stopState = { data: null };
  ctx.renderStopBody = () => { rendered += 1; };
  ctx.updateOfflineText = () => {};
  ctx.refreshView = () => {};
  ctx.setInterval = callback => { tick = callback; };
  vm.runInContext(section("function rerenderCurrentData()", "document.addEventListener(\"visibilitychange\""), ctx);
  assert.equal(typeof tick, "function");
  tick();
  assert.equal(rendered, 1);
});

test("tick ages hidden stop evidence and skips work while hidden", () => {
  const ctx = context(Date.parse("2026-11-04T00:00:00Z"));
  let tick, rendered = 0;
  ctx.document = {
    visibilityState: "hidden", activeElement: null,
    querySelectorAll: () => [],
    querySelector: () => ({ textContent: "Live" }),
    getElementById: () => null,
  };
  ctx.S = { view: "stop", lastLoadAt: 0 };
  ctx.stopState = { data: { items: [{ prediction_source: "heuristic", vehicle_id: "bus",
    data_timestamp: "2026-11-03T23:55:00Z", predicted_arrival_time: "2026-11-04T00:10:00Z" }] } };
  ctx.renderStopBody = () => { rendered++; };
  ctx.updateOfflineText = () => {};
  ctx.refreshView = () => {};
  ctx.setInterval = callback => { tick = callback; };
  vm.runInContext(section("function rerenderCurrentData()", "document.addEventListener(\"visibilitychange\""), ctx);
  tick();
  assert.equal(rendered, 0);
  ctx.document.visibilityState = "visible";
  tick();
  assert.equal(rendered, 1);
  assert.equal(vm.runInContext("freshnessSummary(upcoming(stopState.data))", ctx), "Stale");
});


test("arrival footer preserves freshness messaging and vehicle-specific follow links after merging", () => {
  const ctx = context(Date.parse("2026-11-04T00:00:00Z"));
  ctx.group = [{ route_short_name: "190", trip_id: "trip /1", stop_id: "stop /2", vehicle_id: "bus /3",
    prediction_source: "heuristic", predicted_arrival_time: "2026-11-04T00:10:00Z", data_timestamp: "2026-11-03T23:55:00Z" }];
  const card = vm.runInContext("arrivalCard(group)", ctx);
  assert.match(card, /last bus update is old/);
  assert.match(card, /href="#\/trip\/trip%20%2F1\/stop%20%2F2\/bus%20%2F3"/);
});


test("merged arrival footer keeps sibling countdown markup and escapes provider text", () => {
  const ctx = context(Date.parse("2026-11-04T00:00:00Z"));
  vm.runInContext(section("function esc(str)", "const ACRONYMS"), ctx);
  ctx.group = [
    { headsign: "<img src=x onerror=alert(1)>", prediction_source: "schedule", predicted_arrival_time: "2026-11-04T00:10:00Z" },
    { predicted_arrival_time: "2026-11-04T00:20:00Z" },
  ];
  const card = vm.runInContext("arrivalCard(group)", ctx);
  assert.match(card, /<span class="then">then <span data-countdown=/);
  assert.match(card, /data-countdown-kind="then">20 min<\/span>/);
  assert.match(card, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(card, /<img/);
});
