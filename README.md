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
2. Start the full stack with Docker Compose:

```bash
docker compose up --build
```

This is the recommended way to run the project because the default `DATABASE_URL` uses the Compose service name `db`.

3. Verify ingestion status from inside the app container:

```bash
docker compose exec api uv run python scripts/inspect_ingestion.py
```

4. Verify the API:

```bash
curl http://localhost:8000/v1/health
```

## Local non-Docker workflow

Use this only if you want to run the app directly on the host machine.

1. Install `uv` if it is not already available on your machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Sync dependencies:

```bash
uv sync
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
uv run uvicorn knightbus.main:app --reload
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
    S --> T2[Poll realtime every 30s]
    S --> T3[Refresh features every 5 min]
    S --> T4[Register training run daily]
    T1 --> DB[(PostgreSQL)]
    T2 --> DB
    T3 --> DB
    T4 --> DB
```

## Current behavior

- Loads Prasarana GTFS static ZIP data into PostgreSQL
- Polls Prasarana GTFS-realtime vehicle positions every 30 seconds
- Stores raw and normalized snapshots
- Treats trip updates as optional because the current Prasarana GTFS-R docs expose vehicle positions only
- Serves stop ETAs using schedule + vehicle-progress delay heuristics
- Exposes a placeholder training command for later ML work

## Prasarana feed defaults

- Static GTFS: `https://api.data.gov.my/gtfs-static/prasarana?category=rapid-bus-kl`
- Vehicle positions: `https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-bus-kl`
- Trip updates: not configured by default

The current data.gov.my GTFS-realtime documentation for Prasarana states that vehicle positions are available now, while trip updates and service alerts are still pending. This repo is configured around that shape.

## Layout

- `src/knightbus`: application code
- `scripts/collect.py`: one-off static + realtime collection and feature refresh
- `scripts/inspect_ingestion.py`: print latest ingestion counts and timestamps
- `scripts/train.py`: one-off training entrypoint
- `tests`: basic unit tests
- `AGENTS.md`: repo instructions for future coding agents
- `PLAN.md`: implementation and operating plan

## Where the core logic lives

- Data collection: `src/knightbus/collector.py`
- Scheduled workflows: `src/knightbus/scheduler.py`
- ETL and aggregations: `src/knightbus/etl.py`
- ETA prediction: `src/knightbus/predictor.py`
- Training registration: `src/knightbus/training.py`

## Default deployment shape

- One VM
- One Postgres instance
- One API process
- One worker process using APScheduler
- No Redis, Celery, Airflow, or Kubernetes in v1
