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
- Default scraping path: start `db` and `worker` together so APScheduler can run continuously
- Host-side commands are supported for debugging, but they must point `DATABASE_URL` at `localhost` because `.env` is Compose-oriented and uses the `db` hostname

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
3. Start scraping with `docker compose up -d db worker`
4. For host-only debugging, run `uv run python scripts/collect.py` with `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/knightbus`
5. Verify ingestion with `uv run python scripts/inspect_ingestion.py`
6. Start the API and test `/v1/stops/{stop_id}/etas` with a known stop
