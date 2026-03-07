# Knightbus MVP Plan

## Goal

Collect RapidKL GTFS data, build historical transit observations, and expose bus ETA endpoints for a future client app while keeping build and hosting costs low.

## Scope

- Load GTFS static feed into PostgreSQL
- Poll GTFS-realtime vehicle positions and trip updates every 20-30 seconds
- Store raw snapshots and normalized operational tables
- Serve stop ETAs and route vehicle positions through FastAPI
- Use schedule and heuristic ETA logic first
- Add model registration/training scaffolding without making ML mandatory for launch

## Deployment

- One VM
- Docker Compose
- Services:
  - `api`
  - `worker`
  - `db`

## Schedules

- Realtime poll: every `30 seconds`
- Static feed refresh: every `24 hours`
- Feature refresh: every `5 minutes`
- Training job: every `24 hours`
- Cleanup/retention: add later when data volume grows

## ETA strategy

1. Use GTFS schedule as the baseline
2. Apply trip update arrival times when present
3. Apply realtime delay heuristics when explicit arrival time is absent
4. Add a LightGBM model later once enough data is collected

## Immediate next steps

1. Confirm the RapidKL GTFS static and realtime feed URLs
2. Run `uv sync`
3. Start Postgres and the backend locally
4. Load a static feed snapshot
5. Verify that realtime polling writes snapshots and normalized records
6. Test `/v1/stops/{stop_id}/etas` with a known stop

