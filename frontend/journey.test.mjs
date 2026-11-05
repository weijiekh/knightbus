import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
function setup() {
  const elements = Object.fromEntries(['#trip-body', '#trip-code', '#trip-title'].map(id => [id, { innerHTML: '', querySelectorAll: () => [] }]));
  const ctx = vm.createContext({ $: id => elements[id], esc: String, titleCase: String, ago: () => 'just now', busStale: b => Date.now() - Date.parse(b.observed_at) > 300000, S: {view: 'trip'}, currentRoute: {tripId: 't'}, Date, location: {}, apiFetch: async () => { throw Error('offline'); } });
  vm.runInContext(html.slice(html.indexOf('const tripState ='), html.indexOf('// ══════════ SAVED')), ctx);
  const now = Date.now();
  ctx.data = { trip_id: 't', route_id: 'r', route_short_name: '190', stops: [10,20,30].map((seq,i) => ({stop_sequence:seq,stop_id:`s${i}`,stop_name:`Stop ${i}`})), buses: [
    {vehicle_id:'chosen',trip_id:'t',current_stop_sequence:20,observed_at:new Date(now).toISOString(),arrivals:{30:new Date(now+180000).toISOString()}},
    {vehicle_id:'next',trip_id:'later',current_stop_sequence:10,observed_at:new Date(now).toISOString(),arrivals:{20:new Date(now+240000).toISOString(),30:new Date(now+420000).toISOString()}},
  ]};
  const run = text => vm.runInContext(text, ctx);
  run('tripState.data = data; tripState.selected = "chosen"; tripState.id = "t"');
  return { ctx, run, body: elements['#trip-body'] };
}

test('live progress shows passed, now, upcoming ETA and a following bus at the same stop', () => {
  const {run,body} = setup();
  run('renderTripBody()');
  assert.match(body.innerHTML,/Passed/);
  assert.match(body.innerHTML,/Now · near this stop/);
  assert.match(body.innerHTML,/3 min/);
  assert.match(body.innerHTML,/Next bus here · 7 min/);
  assert.match(body.innerHTML,/2 live buses/);
  assert.doesNotMatch(body.innerHTML,/Back one stop|Bus moved on|tap one to jump|SELECTED/);
  assert.doesNotMatch(body.innerHTML,/href="#\/trip\/t\/s/);
});

test('a disappearing selected bus is not replaced by the next bus', () => {
  const {run,body} = setup();
  run('data.buses.shift(); renderTripBody()');
  assert.equal(run('tripState.selected'), 'chosen');
  assert.match(body.innerHTML,/Waiting for live GPS/);
  assert.doesNotMatch(body.innerHTML,/Passed|aria-current="step"|estimated arrival/);
});

test('failed refresh pauses progress and ETAs instead of displaying cached live information', async () => {
  const {run,body} = setup();
  await run('loadTrip("t")');
  assert.match(body.innerHTML,/Refresh failed/);
  assert.doesNotMatch(body.innerHTML,/aria-current="step"|estimated arrival|Next bus here · 7 min/);
});

test('out-of-order journey responses cannot overwrite newer refresh results', async () => {
  const {ctx,run} = setup();
  let finish;
  ctx.apiFetch = () => new Promise(resolve => { finish = resolve; });
  const first = run('loadTrip("t")');
  const oldFinish = finish;
  const second = run('loadTrip("t")');
  finish({...ctx.data, headsign:'New'});
  await second;
  oldFinish({...ctx.data, headsign:'Old'});
  await first;
  assert.equal(run('tripState.data.headsign'),'New');
});
