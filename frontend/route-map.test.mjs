import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function setup() {
  const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  const start = html.indexOf('const routeState =');
  const source = html.slice(start, html.indexOf('// ══════════ BOOT', start));
  let markers = [];
  const card = { classList: { toggle() {} } };
  const elements = new Map();
  const $ = key => {
    if (!elements.has(key)) elements.set(key, { innerHTML: '', closest: () => card, setAttribute() {}, scrollIntoView() {} });
    return elements.get(key);
  };
  const map = { project: ([lat, lon]) => ({ x: lon, y: lat }), getZoom: () => 12, fitBounds() {}, invalidateSize() {}, on() {}, off() {}, setView() {} };
  const L = { divIcon: x => x, marker: (coords, options) => {
    const marker = { coords, options, addTo() { markers.push(this); return this; }, bindPopup() { return this; }, on() { return this; }, openPopup() { return this; } };
    return marker;
  }};
  const ctx = vm.createContext({ $, window: { L }, L, document: { createElement: () => ({}) }, whenLeaflet: f => f(), S: {}, esc: x => String(x ?? ''), titleCase: String, I: { busLine: '', empty: '' }, ago: () => 'just now', stopDisplay: id => ({ name: id }) });
  vm.runInContext(source, ctx);
  ctx.fakeMap = map;
  ctx.fakeMarkers = { clearLayers() { markers = []; } };
  vm.runInContext('routeMap = fakeMap; routeMarkers = fakeMarkers;', ctx);
  const run = source => vm.runInContext(source, ctx);
  return { ctx, $, run, markers: () => markers };
}

test('direction filtering preserves unknown buses and labels across ordering', () => {
  const { ctx, run } = setup();
  ctx.buses = [{ vehicle_id: 'a', direction_id: 0 }, { vehicle_id: 'b', direction_id: 1 }, { vehicle_id: 'c' }];
  run('routeState.vehicles = buses; buses.forEach(busLabel)');
  run('routeState.direction = "direction:0"');
  assert.equal(run('visibleRouteBuses().length'), 1);
  assert.equal(run('visibleRouteBuses()[0].vehicle_id'), 'a');
  run('routeState.direction = "unknown"');
  assert.equal(run('visibleRouteBuses()[0].vehicle_id'), 'c');
  assert.equal(run('busLabel(buses[1])'), 'B2');
  run('routeState.vehicles.reverse()');
  assert.equal(run('busLabel(buses[1])'), 'B2');
});

test('map groups nearby buses, isolates selection, and fades older GPS', () => {
  const { ctx, run, markers, $ } = setup();
  ctx.buses = [
    { vehicle_id: 'a', lat: 1, lon: 1, observed_at: new Date().toISOString() },
    { vehicle_id: 'b', lat: 2, lon: 2, observed_at: new Date().toISOString() },
    { vehicle_id: 'c', lat: 200, lon: 200, observed_at: '2020-01-01T00:00:00Z' },
  ];
  run('drawRouteMap(buses)');
  assert.equal(markers().length, 2);
  assert.match(markers()[0].options.title, /2 buses/);
  assert.match(markers()[1].options.icon.html, /stale/);
  run('routeState.selected = "b"; drawRouteMap(buses)');
  assert.equal(markers().length, 1);
  assert.match(markers()[0].options.title, /Bus b/);
  assert.equal($('#route-map-all').hidden, false);
  run('routeState.selected = "gone"; drawRouteMap(buses)');
  assert.equal(run('routeState.selected'), null);
  assert.equal(markers().length, 2);
});

test('selected panel and expansion controls track route state', () => {
  const { ctx, run, $ } = setup();
  ctx.buses = [{ vehicle_id: 'a', lat: 1, lon: 1, observed_at: new Date().toISOString(), headsign: 'North', stop_name: 'Station', stop_id: 's', trip_id: 't' }];
  run('routeState.vehicles = buses; routeState.selected = "a"; drawRoute()');
  assert.match($('#selected-bus').innerHTML, /Station/);
  assert.match($('#selected-bus').innerHTML, /Follow this bus/);
  assert.match($('#route-list').innerHTML, /B1/);
  $('#route-map-expand').onclick();
  assert.equal(run('routeState.expanded'), true);
  assert.equal($('#route-map-expand').textContent, 'Collapse map');
  $('#route-map-all').onclick();
  assert.equal($('#selected-bus').innerHTML, '');
});
