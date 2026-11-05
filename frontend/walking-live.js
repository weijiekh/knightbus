/* Browser lifecycle for live walking. Location stays in memory; only route
   requests send coordinates to the existing /v1/walk endpoint. */
const walkNav = {
  id:null, active:false, watchId:null, session:0, model:null, progress:null, fix:null,
  status:'idle', error:null, follow:true, offSince:null, arrivalSince:null,
  lastRerouteAt:-Infinity, rerouting:false, abort:null, startedAt:0,
};
let walkUserMarker = null, walkAccuracyLayer = null;

function clearWalkingWatch() {
  if (walkNav.watchId !== null) navigator.geolocation?.clearWatch(walkNav.watchId);
  walkNav.watchId = null;
  walkNav.session++;
}
function endWalkingNavigation(updateUI = true) {
  clearWalkingWatch();
  walkNav.abort?.abort(); walkNav.abort = null;
  walkNav.active = false; walkNav.rerouting = false; walkNav.status = 'idle';
  walkNav.error = null; walkNav.offSince = null; walkNav.arrivalSince = null;
  if (updateUI && S.view === 'stop') renderLiveWalkingPanel();
}
function startWalkingNavigation() {
  if (S.view !== 'stop' || stopState.mode !== 'map' || walkState.id !== stopState.id || !walkState.route) return;
  endWalkingNavigation(false);
  walkNav.id = stopState.id;
  walkNav.model = WalkNavigation.build(walkState.route);
  walkNav.progress = null; walkNav.fix = null; walkNav.follow = true; walkNav.lastRerouteAt = -Infinity;
  if (!walkNav.model || !navigator.geolocation?.watchPosition) {
    walkNav.error = !walkNav.model ? 'Live guidance is unavailable for this route. Use the directions or open Maps.' : 'Live GPS is unavailable in this browser. Open Maps for navigation.';
    renderLiveWalkingPanel(); return;
  }
  walkNav.active = true;
  watchWalkingPosition();
}
function watchWalkingPosition() {
  clearWalkingWatch();
  const session = walkNav.session;
  walkNav.status = 'locating'; walkNav.error = null; walkNav.startedAt = Date.now();
  try {
    walkNav.watchId = navigator.geolocation.watchPosition(
      pos => {
        if (session !== walkNav.session || !walkNav.active || S.view !== 'stop' || stopState.id !== walkNav.id || stopState.mode !== 'map' || document.visibilityState !== 'visible') return;
        updateWalkingPosition({lat:pos.coords.latitude, lon:pos.coords.longitude, accuracy:pos.coords.accuracy, timestamp:pos.timestamp});
      },
      error => {
        if (session !== walkNav.session || !walkNav.active) return;
        if (error.code === 1) {
          endWalkingNavigation(false);
          walkNav.error = 'Location access was denied. Enable it and start navigation again.';
        } else {
          walkNav.status = 'weak'; walkNav.offSince = null; walkNav.arrivalSince = null;
          walkNav.error = 'GPS signal lost. Turn updates are paused until your location returns.';
        }
        renderLiveWalkingPanel();
      },
      {enableHighAccuracy:true, maximumAge:0, timeout:15000},
    );
  } catch {
    endWalkingNavigation(false);
    walkNav.error = 'Live GPS could not start. Check location access or open Maps for navigation.';
  }
  renderLiveWalkingPanel();
}
function updateWalkingPosition(fix) {
  if (walkNav.fix && fix.timestamp <= walkNav.fix.timestamp) return;
  const result = WalkNavigation.update(walkNav.model, fix, walkNav.progress, stopCoords(walkNav.id));
  walkNav.status = result.status;
  if (result.status === 'weak') {
    walkNav.error = 'Waiting for an accurate, fresh GPS location. Turn updates are paused.';
    walkNav.offSince = null; walkNav.arrivalSince = null;
    renderLiveWalkingPanel(); return;
  }
  walkNav.fix = fix;
  if (result.status === 'onroute') walkNav.error = null;
  S.loc = {lat:fix.lat, lon:fix.lon, gps:true, label:'Your location'};
  drawWalkingPosition(walkNav.follow);
  if (result.status === 'offroute') {
    walkNav.arrivalSince = null;
    walkNav.offSince ??= fix.timestamp;
    if (fix.timestamp - walkNav.offSince >= 5000 && !walkNav.rerouting && Date.now() - walkNav.lastRerouteAt >= 30000) rerouteWalking();
  } else {
    // A route requested while off course is obsolete once the rider rejoins
    // the current path. Abort it without replacing the GPS watch session.
    if (walkNav.rerouting) {
      walkNav.abort?.abort(); walkNav.abort = null; walkNav.rerouting = false;
    }
    walkNav.offSince = null;
    walkNav.progress = result;
    if (result.nearArrival) {
      walkNav.arrivalSince ??= fix.timestamp;
      if (fix.timestamp - walkNav.arrivalSince >= 3000) {
        clearWalkingWatch(); walkNav.active = false; walkNav.status = 'arrived';
        walkNav.abort?.abort(); walkNav.abort = null; walkNav.rerouting = false;
      }
    } else walkNav.arrivalSince = null;
  }
  renderLiveWalkingPanel();
}
async function rerouteWalking() {
  if (!walkNav.active || walkNav.rerouting || !walkNav.fix) return;
  const id = walkNav.id, session = walkNav.session, destination = stopCoords(id), from = walkNav.fix;
  if (!destination) return;
  walkNav.rerouting = true; walkNav.lastRerouteAt = Date.now();
  const controller = new AbortController(); walkNav.abort = controller;
  const isCurrent = () => session === walkNav.session && walkNav.active && walkNav.id === id && walkNav.abort === controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  renderLiveWalkingPanel();
  try {
    const q = new URLSearchParams({from_lat:from.lat, from_lon:from.lon, to_lat:destination.lat, to_lon:destination.lon});
    const response = await fetch(`${API_BASE}/v1/walk?${q}`, {signal:controller.signal});
    if (!response.ok) throw new Error('routing failed');
    const route = await response.json(), model = WalkNavigation.build(route);
    if (!model) throw new Error('route has no usable geometry');
    if (!isCurrent() || controller.signal.aborted) return;
    walkState.route = route; walkNav.model = model; walkNav.progress = null;
    walkNav.offSince = null; walkNav.arrivalSince = null; walkNav.error = null;
    walkNav.status = 'locating';
    renderWalkBar(); drawWalkLayer(false);
  } catch (error) {
    if (!isCurrent()) return;
    walkNav.error = 'Could not reroute. Retrying when GPS is off route; you can also open Maps.';
  } finally {
    clearTimeout(timeout);
    if (isCurrent()) {
      walkNav.rerouting = false; walkNav.abort = null;
      renderLiveWalkingPanel();
    }
  }
}
function checkWalkingGPS() {
  if (!walkNav.active || document.visibilityState !== 'visible') return;
  const last = walkNav.fix?.timestamp || walkNav.startedAt;
  if (Date.now() - last > 20000) {
    walkNav.status = 'weak'; walkNav.error = 'Waiting for fresh GPS. Turn updates are paused.';
    walkNav.offSince = null; walkNav.arrivalSince = null;
    renderLiveWalkingPanel();
  }
}
function walkingVisibilityChanged() {
  if (!walkNav.active) return;
  if (document.visibilityState !== 'visible') {
    clearWalkingWatch(); walkNav.abort?.abort(); walkNav.abort = null; walkNav.rerouting = false;
    walkNav.status = 'paused'; walkNav.offSince = null; walkNav.arrivalSince = null;
  } else {
    // Wait for a fresh fix before showing guidance, but retain route progress
    // as a matching anchor so a crossing cannot reset us to an earlier loop.
    walkNav.fix = null;
    watchWalkingPosition();
  }
}
function drawWalkingPosition(follow = false) {
  if (!stopMap || !window.L) return;
  const fix = walkNav.active && walkNav.id === stopState.id ? walkNav.fix : null;
  const position = fix || S.loc;
  if (!position) return;
  const coords = [position.lat, position.lon];
  if (!walkUserMarker) {
    walkUserMarker = L.marker(coords, {icon:L.divIcon({className:'', html:'<div class="me-dot"></div>', iconSize:[22,22], iconAnchor:[11,11]}), keyboard:false, interactive:false, title:'Your GPS location'}).addTo(stopMap);
  } else walkUserMarker.setLatLng(coords);
  if (fix) {
    if (!walkAccuracyLayer) walkAccuracyLayer = L.circle(coords, {radius:fix.accuracy, color:'#5b9cf6', weight:1, fillOpacity:0.08, interactive:false}).addTo(stopMap);
    else { walkAccuracyLayer.setLatLng(coords); walkAccuracyLayer.setRadius(fix.accuracy); }
  }
  if (follow && fix) stopMap.setView(coords, Math.max(stopMap.getZoom(), 17), {animate:false});
}
function recenterWalkingNavigation() {
  walkNav.follow = true; drawWalkingPosition(true); renderLiveWalkingPanel();
}
function renderLiveWalkingPanel() {
  const panel = $('#walk-nav');
  if (!panel) return;
  const active = walkNav.active && walkNav.id === stopState.id;
  const arrived = walkNav.id === stopState.id && walkNav.status === 'arrived';
  let headline = 'Ready to walk?', detail = 'Start live guidance to update turns as you walk. Keep this page open.';
  let symbol = '↑';
  if (arrived) { headline = 'You’re near your stop'; detail = 'Check the stop name and the side of the road before boarding.'; symbol = '⚑'; }
  else if (active && walkNav.rerouting) { headline = 'Finding a new walking route…'; detail = 'Your position is still updating.'; }
  else if (active && walkNav.status === 'offroute') { headline = 'You appear to be off route'; detail = 'Checking GPS before finding a new route.'; }
  else if (active && ['locating','weak','paused'].includes(walkNav.status)) {
    headline = walkNav.status === 'locating' ? 'Finding your location…' : walkNav.status === 'paused' ? 'Navigation paused' : 'Waiting for GPS…';
    detail = walkNav.error || 'Turn updates will resume with a fresh location.';
  } else if (active && walkNav.progress) {
    const p = walkNav.progress, steps = walkNav.model.steps, current = steps[p.stepIndex], next = steps[p.stepIndex + 1];
    headline = next && p.turnDistance <= 40 ? `In ${fmtDist(p.turnDistance)} · ${next.instruction}` : current?.instruction || 'Follow the walking route on the map';
    symbol = walkTurnIcon((next && p.turnDistance <= 40 ? next : current)?.type);
    const minutes = Number.isFinite(walkNav.model.duration) ? Math.max(1, Math.ceil(walkNav.model.duration * p.remaining / walkNav.model.total / 60)) : null;
    detail = `${fmtDist(p.remaining)} remaining${minutes ? ` · about ${minutes} min` : ''}${next && p.turnDistance > 40 ? ` · then ${next.instruction} in ${fmtDist(p.turnDistance)}` : ''}`;
    if (!steps.length) detail += ' · Turn updates unavailable for this route';
  }
  const focusAction = panel.contains(document.activeElement) ? document.activeElement?.dataset.walkNav : null;
  panel.innerHTML = `<div class="walk-guidance"><span class="walk-turn" aria-hidden="true">${symbol}</span><div class="grow"><div class="walk-instruction" role="status" aria-live="polite">${esc(headline)}</div><div class="psub">${esc(detail)}</div></div></div>
    ${walkNav.error && !['weak','paused'].includes(walkNav.status) ? `<p class="psub" role="status">${esc(walkNav.error)}</p>` : ''}
    <div class="walk-actions">${active ? `<button type="button" class="btn out" data-walk-nav="end">End navigation</button><button type="button" class="btn out" data-walk-nav="recenter">${walkNav.follow ? 'Following you' : 'Recenter'}</button>` : arrived ? `<button type="button" class="btn" data-walk-nav="arrivals">See bus arrivals</button>` : `<button type="button" class="btn" data-walk-nav="start">Start live navigation</button>`}</div>`;
  if (focusAction) panel.querySelector(`[data-walk-nav="${focusAction}"]`)?.focus({preventScroll:true});
  document.querySelectorAll('[data-walk-step]').forEach(row => {
    const current = active && walkNav.status === 'onroute' && !walkNav.rerouting && Number(row.dataset.walkStep) === walkNav.progress?.stepIndex;
    row.classList.toggle('walk-current', current);
    if (current) row.setAttribute('aria-current','step'); else row.removeAttribute('aria-current');
  });
}
