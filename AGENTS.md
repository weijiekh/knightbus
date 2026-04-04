# AGENTS.md

## Purpose

This repo contains the Knightbus RapidKL ETA backend. Favor small, production-usable increments over platform-heavy abstractions.

## Technical defaults

- Use `Python 3.12`
- Use `uv` for dependency management and command execution
- Keep the backend as a single Python codebase
- Use FastAPI for HTTP endpoints
- Use PostgreSQL for application data
- Use APScheduler for recurring jobs

## Architecture rules

- Keep v1 as one deployable backend plus Postgres
- Do not introduce Redis, Celery, Kafka, Airflow, or Kubernetes unless explicitly requested
- Prefer schedule + heuristic ETA behavior over speculative ML complexity
- Treat GTFS ingestion and normalization as first-class production code, not notebook logic
- When a GTFS-realtime feed omits `stop_id`, the collector infers it by matching vehicle GPS to the nearest stop on the route within 200m (`STOP_MATCH_RADIUS_METERS` in `collector.py`)
- Keep training code isolated so it can stay optional in low-cost deployments
- ML training requires at least 5,000 labeled rows (ideally 1–2 weeks of vehicle position history) before activating
- Training is skipped if the newest data is older than 48 hours to prevent stale models
- Training metrics include per-route and per-stop coverage; check `routes_below_100` and `stops_below_50` to identify under-represented categories

## Development workflow

- Install dependencies with `uv sync`
- Default local runtime is `docker compose up --build`
- Run the API with `uv run uvicorn knightbus.main:app --reload`
- Run the worker with `uv run python -m knightbus.scheduler`
- Run one-off collection with `uv run python scripts/collect.py`
- Inspect ingestion with `uv run python scripts/inspect_ingestion.py`
- Run tests with `uv run pytest`
- Use environment variables from `.env`
- Configure multiple GTFS feeds via the `GTFS_FEEDS` JSON env var (see `.env.example`); falls back to legacy single-feed vars if not set
- When running from the host instead of Docker Compose, override `DATABASE_URL` to use `localhost` instead of `db`
- If `uv` cannot write to its default cache in a sandboxed environment, set `UV_CACHE_DIR` to a writable project-local path such as `.uv-cache`
- Realtime polling is service-window-aware by default: `06:00-00:00` local time at the normal cadence, then a slower off-hours health poll
- Deploy to EC2: see `deploy/README.md` for sizing and launch instructions
- Bootstrap a new instance with `deploy/ec2-user-data.sh` (passed as EC2 User Data)
- Update a running instance with `deploy/update.sh`

## Code conventions

- Keep modules focused by responsibility: `collector`, `etl`, `predictor`, `training`, `api`
- Add shared utilities to `src/knightbus`, not ad hoc scripts
- Prefer explicit schemas and typed function signatures
- Avoid premature abstractions for provider plugins or multi-tenant support

## Operational priorities

- Correctness of feed ingestion
- Clear fallback behavior when realtime data is missing
- Low monthly cost
- Simple deployment on one VM
- Prefer starting scraping through the `worker` service so APScheduler keeps polling continuously
- Prefer limiting high-frequency realtime polling to the configured service window instead of shutting the worker down entirely
