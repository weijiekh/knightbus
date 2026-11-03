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

function fixture() {
  const node = () => ({ innerHTML: "", hidden: false, querySelectorAll: () => [] });
  const elements = Object.fromEntries([
    "#filters", "#results", "#recent", "#retry-search", "#trip-body", "#saved-list", "#saved-routes",
  ].map(key => [key, node()]));
  const context = vm.createContext({
    $: key => elements[key], esc: String, AbortController, URLSearchParams,
    S: { view: "search", recent: [], saved: ["s1"], routeMeta: {} },
    searchState: { q: "190", stops: [], routes: [], places: [], filter: "all", ctrl: {}, loading: true, error: false },
    apiFetch: async () => { throw new Error("offline"); },
    onQuery() {}, tripState: { data: { stops: [] } },
    savedState: { etas: {} }, savedRoute: "190", savedDrag: null,
    savedRouteLabels: () => [], syncSavedReorder() {}, servesRoute: () => false,
    savedCard: () => "", I: {}, titleCase: String,
  });
  vm.runInContext(source("async function searchApi(q)", "async function searchPlaces(q)"), context);
  vm.runInContext(source("function resultList()", "function resultRow("), context);
  vm.runInContext(source("function renderTripBody(stopId)", "// ══════════ SAVED"), context);
  vm.runInContext(source("function drawSaved()", "// Top 1–2 routes"), context);
  return { context, elements };
}

test("search distinguishes an API outage from an empty successful search", async () => {
  const { context, elements } = fixture();
  await vm.runInContext('searchApi("190")', context);
  assert.match(elements["#results"].innerHTML, /search unavailable/);
  assert.match(elements["#results"].innerHTML, /Retry search/);
  assert.doesNotMatch(elements["#results"].innerHTML, /No matches/);
  context.apiFetch = async () => ({ stops: [], routes: [] });
  context.searchState.error = false; // onQuery clears this before a retry.
  await vm.runInContext('searchApi("190")', context);
  assert.match(elements["#results"].innerHTML, /No matches/);
  assert.doesNotMatch(elements["#results"].innerHTML, /search unavailable/);
});

test("failed requests from an old query do not mark the current query as failed", async () => {
  const { context } = fixture();
  context.searchState.q = "191";
  await vm.runInContext('searchApi("190")', context);
  assert.equal(context.searchState.error, false);
});

test("empty trips show a navigation action instead of accessing a missing stop", () => {
  const { context, elements } = fixture();
  vm.runInContext('renderTripBody("missing")', context);
  assert.match(elements["#trip-body"].innerHTML, /No stops available/);
  assert.match(elements["#trip-body"].innerHTML, /href="#\/routes"/);
});

