# Knightbus

Backend for collecting GTFS static/realtime data and serving RapidKL bus ETA predictions.

## Stack

- Python 3.12
- `uv` for dependency management
- FastAPI
- PostgreSQL
- APScheduler
- SQLAlchemy

## Quick start

1. Copy `.env.example` to `.env`. It is prefilled for Prasarana `rapid-bus-kl` on data.gov.my.
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

- Loads Prasarana GTFS static ZIP data into PostgreSQL
- Polls Prasarana GTFS-realtime vehicle positions every 30 seconds during service hours and every 15 minutes overnight
- Stores raw and normalized snapshots
- Treats trip updates as optional because the current Prasarana GTFS-R docs expose vehicle positions only
- Serves stop ETAs using schedule + vehicle-progress delay heuristics
- Optionally applies a LightGBM residual model when trained
  - Training labels are derived from vehicle position proximity to GTFS stop coordinates
  - Requires at least 5,000 labeled rows (ideally 1–2 weeks of data) before training activates
  - Skips training if the newest data is older than 48 hours to avoid stale models
  - Training metrics include per-route and per-stop coverage to flag under-represented categories

## Prasarana feed defaults

- Static GTFS: `https://api.data.gov.my/gtfs-static/prasarana?category=rapid-bus-kl`
- Vehicle positions: `https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-bus-kl`
- Trip updates: not configured by default

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

- One VM
- One Postgres instance
- One API process
- One worker process using APScheduler
- No Redis, Celery, Airflow, or Kubernetes in v1
