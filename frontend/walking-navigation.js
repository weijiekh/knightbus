/* GPS progress on a pedestrian route. No browser state or network requests here. */
(() => {
  const RAD = Math.PI / 180, R = 6371000;
  function distance(a, b) {
    const dy = (b.lat - a.lat) * RAD, dx = (b.lon - a.lon) * RAD;
    const h = Math.sin(dy / 2) ** 2 + Math.cos(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.sin(dx / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function validPoint(p) {
    return p && Number.isFinite(p.lat) && Number.isFinite(p.lon) && Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180;
  }
  function build(route) {
    if (!Array.isArray(route?.geometry) || !route.geometry.every(p => Array.isArray(p) && p.length >= 2)) return null;
    const points = route.geometry.map(p => ({lat:p[0], lon:p[1]}));
    if (points.length < 2 || !points.every(validPoint)) return null;
    const cumulative = [0];
    for (let i = 1; i < points.length; i++) cumulative.push(cumulative[i - 1] + distance(points[i - 1], points[i]));
    if (cumulative.at(-1) < 1) return null;
    const steps = Array.isArray(route.steps) ? route.steps : [];
    // Never infer turn locations by distributing the provider's distances.
    let lastEnd = 0;
    const mapped = steps.length && steps.every(step => {
      const pair = step?.way_points;
      if (!Array.isArray(pair) || pair.length !== 2 || !pair.every(Number.isInteger)
          || pair[0] !== lastEnd || pair[1] < pair[0] || pair[1] >= points.length) return false;
      lastEnd = pair[1];
      return true;
    }) && lastEnd === points.length - 1;
    return {points, cumulative, total:cumulative.at(-1), steps:mapped ? steps : [], duration:route.duration_s};
  }
  function locate(model, point, previous = null) {
    const candidates = [];
    const scaleX = R * RAD * Math.cos(point.lat * RAD), scaleY = R * RAD;
    for (let i = 0; i < model.points.length - 1; i++) {
      const a = model.points[i], b = model.points[i + 1];
      const ax = (a.lon - point.lon) * scaleX, ay = (a.lat - point.lat) * scaleY;
      const dx = (b.lon - a.lon) * scaleX, dy = (b.lat - a.lat) * scaleY;
      const lengthSquared = dx * dx + dy * dy;
      const t = lengthSquared ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / lengthSquared)) : 0;
      candidates.push({distance:Math.hypot(ax + t * dx, ay + t * dy), along:model.cumulative[i] + t * (model.cumulative[i + 1] - model.cumulative[i])});
    }
    const nearest = Math.min(...candidates.map(p => p.distance));
    // At a crossing, prefer continuity instead of jumping to a later loop.
    const close = candidates.filter(p => p.distance <= nearest + 5);
    close.sort((a, b) => Math.abs(a.along - (previous?.along || 0)) - Math.abs(b.along - (previous?.along || 0)));
    return close[0];
  }
  function update(model, fix, previous, destination, now = Date.now()) {
    if (!validPoint(fix) || !Number.isFinite(fix.accuracy) || fix.accuracy < 0 || fix.accuracy > 40
        || !Number.isFinite(fix.timestamp) || now - fix.timestamp > 20000 || fix.timestamp > now + 5000
        || (previous && fix.timestamp <= previous.timestamp)) return {status:'weak'};
    const match = locate(model, fix, previous);
    const threshold = Math.max(35, fix.accuracy * 2);
    if (match.distance > threshold) return {status:'offroute', distance:match.distance};
    // A single fix must not teleport progress to another part of a looping route.
    if (previous) {
      const seconds = Math.max(0, (fix.timestamp - previous.timestamp) / 1000);
      if (Math.abs(match.along - previous.along) > Math.max(50, seconds * 4 + fix.accuracy * 2)) return {status:'weak'};
    }
    const remaining = Math.max(0, model.total - match.along);
    let stepIndex = model.steps.findIndex(step => model.cumulative[step.way_points[1]] > match.along + 5);
    if (stepIndex < 0 && model.steps.length) stepIndex = model.steps.length - 1;
    const next = model.steps[stepIndex + 1];
    const turnDistance = next ? Math.max(0, model.cumulative[next.way_points[0]] - match.along) : null;
    const nearStop = validPoint(destination) && distance(fix, destination) <= 20;
    return {status:'onroute', along:match.along, remaining, stepIndex, turnDistance, timestamp:fix.timestamp,
      nearArrival:remaining <= 20 && nearStop && fix.accuracy <= 25};
  }
  globalThis.WalkNavigation = Object.freeze({build, update, distance});
})();
