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
- Model artifact is a LightGBM booster string; the API predicts without pandas/sklearn
- Static GTFS ingest streams with batched Core inserts; GCP `mem_limit`s are db 128m, api 192m, worker 128m
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
- Deploy target: shared GCP e2-micro `krappa-haus` (us-central1-a); runbook `deploy/gcp/README.md`
- GCP compose: `COMPOSE_ARGS="-f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml" deploy/update.sh`; `PRUNE_BUILD_CACHE=1` also runs `docker builder prune -f` (shared host, clears all projects' cache)
- GCP ports: Postgres `127.0.0.1:5433`, API `127.0.0.1:8100`, host nginx `knightbus.34.123.154.175.sslip.io` -> `127.0.0.1:8100`; nothing on `0.0.0.0`
- Frontend: Cloudflare Pages (`manabas.pages.dev`, fallback `basmana.pages.dev`); `frontend/functions/v1/[[path]].js` proxies `/v1/*` to Pages env `API_ORIGIN`
- Training: `.github/workflows/train.yml` runs weekly (Mon 03:00 MYT) and via `workflow_dispatch`; worker training is off on GCP (`TRAINING_IN_WORKER=false`)
- Dockerfile build arg `ML_DEPS=full|inference|none` (GCP: `api` inference, `worker` none)
- New env vars: `TRAINING_IN_WORKER` (default true), `REALTIME_RETENTION_DAYS` (14), `RAW_SNAPSHOT_RETENTION_DAYS` (2), `POSTGRES_PASSWORD`, `KNIGHTBUS_DB_PORT`, `KNIGHTBUS_API_PORT`
- Legacy EC2: `deploy/README.md` and `deploy/ec2-user-data.sh` still work with `docker compose --profile web`
- Update a running instance with `deploy/update.sh`
- CI/CD: `.github/workflows/ci.yml` tests PRs; push to `main` builds `ghcr.io` api/worker images in Actions and deploys via `deploy/gcp/deploy.sh` as `knightbus-deploy` (never build on the VM)

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
