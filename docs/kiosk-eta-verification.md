# RapidKL kiosk comparison — 15 November 2026

Captured with Camoufox network response listeners around 22:01–22:03 MYT.

## Official source

Page: https://myrapidbus.prasarana.com.my/kiosk?route=1036&bus=

- Kiosk route `1036` is T565. Its operational AVL code is `T5650`.
- Initial snapshot: `GET https://myrapidbus.prasarana.com.my/avl/api/avl/region/rkl`.
- Updates: Pusher-compatible websocket at `/avl/app/<public-app-key>`, channel
  `avl.region.rkl`, event `avl.data`.
- The page filters positions using `dt_gps`, interpreted in Asia/Kuala_Lumpur,
  accepting GPS up to five minutes old and up to one minute in the future.
- Snapshot contained VH2491 (GPS 21:59:12, 3.022439, 101.708895) and VK2853
  (GPS 22:01:21, 3.021639, 101.709237), both on `T5650`.
- The page's `updateEta()` calls `POST /website/getBusstopEta` with `busstopId`
  **only when `no_route == 'U8210'`**. It does not request arrival predictions
  for T565. Therefore this route verifies bus presence/positions, not official
  arrival-minute accuracy.

## Production comparison

Production search maps T565 to static route `30000174`, feed
`rapid-bus-mrtfeeder`, terminal stop `12003070`.

At 22:02:41 MYT, `/v1/stops/12003070/etas?route_id=30000174` returned:

| Scheduled arrival (MYT) | Predicted arrival (MYT) | Source | Vehicle |
| --- | --- | --- | --- |
| 22:05 | 22:02:40 (0 minutes) | ml_residual | VH2491 |
| 22:30 | 22:10:21 | ml_residual | none |
| 23:05 | 22:22:30 | ml_residual | none |
| 23:30 | 22:38:27 | ml_residual | none |
| 00:05 next day | 22:11:20 (8 minutes) | ml_residual | none |

The route vehicles endpoint for `30000174` returned no buses. Querying raw
route code `T565` returned VH2491 with `(0, 0)`, no stop sequence and a collection
timestamp of 22:00:25. The predictor could still select an older matched row.
This demonstrates both an operational/static route identifier mismatch and
reuse of superseded position evidence.

The contemporaneous government GTFS snapshots contained 54 KL and 81
MRT-feeder entities; neither contained VH2491, VK2853 or a T565 route entry.
This is a point-in-time coverage difference, not proof that the feed always
omits this route.

## Local changes

- Preserve timetable times when no current realtime evidence supports an estimate.
- Suppress estimates outside the route's scheduled operating span.
- Remove the fallback to expired trips after the last bus.
- Include delayed live trips independently of scheduled candidate times and sort
  by predicted arrival.
- Rank all recent positions per bus before requiring a matched stop; reject
  stale/future GPS and invalid `(0, 0)` positions without reviving older rows.
- Match route vehicles through the static trip's route as well as the raw route
  identifier; expose GPS time for freshness.

The optional kiosk collector now supports shadow comparison and GPS supplementation;
it is disabled by default. See `infra/gcp/README.md` for rollout and fallback settings.
Absent reliable trip/progress evidence, arrivals stay timetable-only. Source coverage
comparison alone does not validate arrival-minute accuracy.
