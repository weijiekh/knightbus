# Knightbus

Backend for collecting GTFS static/realtime data and serving bus ETA predictions. Supports multiple GTFS feeds from any provider worldwide.

## Stack

- Python 3.12
- `uv` for dependency management
- FastAPI
- PostgreSQL
- APScheduler
- SQLAlchemy

## Quick start

1. Copy `.env.example` to `.env`. It is prefilled for three Prasarana feeds (`rapid-bus-kl`, `rapid-bus-mrtfeeder`, `rapid-bus-penang`) on data.gov.my.
2. Start the full stack:

```bash
docker compose up --build -d
```

Docker Compose is the default workflow for this project. The default `DATABASE_URL` uses the Compose service name `db`, so running the stack in containers avoids host/container networking mismatches.

3. Check container status:

```bash
docker compose ps
```

4. Inspect ingestion status:

```bash
docker compose exec worker uv run python scripts/inspect_ingestion.py
```

5. Verify the API:

```bash
curl http://localhost:56709/v1/health
```

6. Trigger a manual training run (LightGBM optional, labels derived from vehicle positions):

```bash
docker compose exec worker uv run python scripts/train.py
```

Training uses vehicle position history and GTFS stop coordinates to derive arrival labels.

7. Inspect registered training runs:

```bash
docker compose exec db psql -U postgres -d knightbus -c "select version, status, created_at from model_versions order by created_at desc limit 10;"
```

8. List known stops and test an ETA endpoint:

```bash
docker compose exec db psql -U postgres -d knightbus -c "select stop_id, name from stops limit 20;"
curl "http://localhost:56709/v1/stops/STOP_ID/etas"
```

## Common Docker commands

Start or restart a single service:

```bash
docker compose up -d api
docker compose up -d worker
docker compose up -d db
```

View logs:

```bash
docker compose logs -f worker
docker compose logs -f api
```

If a container exists but is not running:

```bash
docker compose start api
docker compose start worker
```

## Local non-Docker workflow

Use this only if you explicitly want to run the app on the host machine instead of in Docker.

1. Install `uv` if it is not already available on your machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Sync dependencies:

```bash
uv sync
```

If you want ML training/inference, install optional ML dependencies:

```bash
uv sync --extra ml
```

3. Update `DATABASE_URL` in `.env` to use `localhost` instead of `db`:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/knightbus
```

4. Start Postgres:

```bash
docker compose up db -d
```

5. Start the API locally:

```bash
uv run uvicorn knightbus.main:app --host 0.0.0.0 --port 56709 --reload
```

6. Start the scheduler/collector worker:

```bash
uv run python -m knightbus.scheduler
```

7. Run one-off data collection manually:

```bash
uv run python scripts/collect.py
```

8. Run one-off training manually:

```bash
uv run python scripts/train.py
```

9. Inspect latest ingestion counts and timestamps:

```bash
uv run python scripts/inspect_ingestion.py
```

10. Run tests:

```bash
uv run pytest
```

## Architecture

```mermaid
flowchart LR
    A[GTFS Static Feed] --> C[Collector]
    B[GTFS Realtime Feed] --> C
    C --> D[(PostgreSQL)]
    D --> E[ETL / Aggregations]
    E --> D
    D --> F[Predictor]
    F --> G[FastAPI]
    G --> H[Client App]
    D --> I[Training Job]
    I --> D
```

## Runtime workflow

```mermaid
flowchart TD
    S[APScheduler] --> T1[Refresh static feed daily]
    S --> T2[Poll realtime every 30s during service hours]
    S --> T5[Poll realtime every 15 min overnight]
    S --> T3[Refresh features every 5 min]
    S --> T4[Register training run daily]
    T1 --> DB[(PostgreSQL)]
    T2 --> DB
    T5 --> DB
    T3 --> DB
    T4 --> DB
```

## Current behavior

- Collects from multiple GTFS feeds in parallel (configured via `GTFS_FEEDS` env var)
- Loads GTFS static ZIP data into PostgreSQL, scoped by feed name
- Polls GTFS-realtime vehicle positions every 30 seconds during service hours and every 15 minutes overnight
- Stores raw and normalized snapshots
- Infers `stop_id` via GPS proximity matching (within 200m) when the feed does not provide it
- Treats trip updates as optional because the current Prasarana GTFS-R docs expose vehicle positions only
- Serves stop ETAs using schedule + vehicle-progress delay heuristics
- Optionally applies a LightGBM residual model when trained
  - Training labels are derived from vehicle position proximity to GTFS stop coordinates
  - Requires at least 5,000 labeled rows (ideally 1–2 weeks of data) before training activates
  - Skips training if the newest data is older than 48 hours to avoid stale models
  - Training metrics include per-route and per-stop coverage to flag under-represented categories

## Multi-feed configuration

Set `GTFS_FEEDS` as a JSON array of feed objects, each with `name`, `static_url`, `vehicle_positions_url`, and optionally `trip_updates_url`. This works with any GTFS provider worldwide.

Example for a non-Prasarana feed:

```env
GTFS_FEEDS=[{"name":"my-city-bus","static_url":"https://example.com/gtfs-static.zip","vehicle_positions_url":"https://example.com/gtfs-rt/vehicle-positions"}]
```

If `GTFS_FEEDS` is not set, the system falls back to the legacy single-feed env vars (`GTFS_STATIC_URL`, `GTFS_VEHICLE_POSITIONS_URL`, `GTFS_TRIP_UPDATES_URL`) as a feed named `"default"`.

## Frontend

A single-file HTML app at `frontend/index.html`, served by the `web` (nginx) container on port 80.

Features:
- GPS-based or manual place search (Nominatim/OpenStreetMap) to set location
- Finds the 5 nearest stops within 500m
- Fetches ETAs for each stop in parallel
- Dark/light mode toggle, persisted in localStorage
- Auto-refreshes every 30 seconds
- Prediction source badges: `sched` (gray) / `live` (blue) / `ml` (green)

The nginx container proxies `/v1/*` to the `api` container internally — no extra ports needed. Access at `http://<your-server>` (port 80).

Production frontend is on Cloudflare Pages, which proxies `/v1/*` to the API. See `infra/gcp/README.md` for the GCP setup. The EC2 path in `deploy/README.md` is legacy.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/health` | Health check |
| GET | `/v1/stops/nearby?lat=&lon=&radius_m=&limit=` | Stops near a GPS coordinate, sorted by distance |
| GET | `/v1/stops/{stop_id}/etas` | ETAs for a specific stop |
| GET | `/v1/routes/{route_id}/vehicles` | Live vehicle positions for a route |
| GET | `/v1/models/current` | Current ML model version and status |

## Prasarana feed defaults

The default `.env.example` configures three Prasarana feeds:

- `rapid-bus-kl` — RapidKL bus (Kuala Lumpur)
- `rapid-bus-mrtfeeder` — MRT feeder buses
- `rapid-bus-penang` — RapidPenang bus

The current data.gov.my GTFS-realtime documentation for Prasarana states that vehicle positions are available now, while trip updates and service alerts are still pending. This repo is configured around that shape.

## Realtime polling window

- Service timezone defaults to `Asia/Kuala_Lumpur`
- Active poll window defaults to `06:00-00:00` local time
- Active poll interval defaults to `30 seconds`
- Off-hours health poll interval defaults to `15 minutes`
- Configure these with:
  - `GTFS_SERVICE_TIMEZONE`
  - `GTFS_ACTIVE_POLL_START_LOCAL`
  - `GTFS_ACTIVE_POLL_END_LOCAL`
  - `GTFS_OFF_HOURS_POLL_INTERVAL_SECONDS`

## Layout

- `src/knightbus`: application code
- `scripts/collect.py`: one-off static + realtime collection and feature refresh
- `scripts/inspect_ingestion.py`: print latest ingestion counts and timestamps
- `scripts/train.py`: one-off training entrypoint (LightGBM optional)
- `tests`: basic unit tests
- `AGENTS.md`: repo instructions for future coding agents
- `PLAN.md`: implementation and operating plan

## Where the core logic lives

- Data collection: `src/knightbus/collector.py`
- Scheduled workflows: `src/knightbus/scheduler.py`
- ETL and aggregations: `src/knightbus/etl.py`
- ETA prediction: `src/knightbus/predictor.py`
- Training and model registry: `src/knightbus/training.py`
- Feature extraction for ML: `src/knightbus/features.py`
- Model load/save helpers: `src/knightbus/ml.py`

## Default deployment shape

- Shared GCP e2-micro (1 GB), Docker Compose: `infra/gcp/compose.yaml` (prebuilt images, deployed by `.github/workflows/deploy.yml`). Runbook: `infra/gcp/README.md`
- One Postgres instance and one API process that also runs APScheduler (`EMBEDDED_SCHEDULER=true`; each with a `mem_limit`). Local `docker compose` still uses a separate `worker`
- Frontend on Cloudflare Pages, calling the API through a Pages Function
- Weekly model training in GitHub Actions (`.github/workflows/train.yml`), not in the API/worker
- No Redis, Celery, Airflow, or Kubernetes in v1
