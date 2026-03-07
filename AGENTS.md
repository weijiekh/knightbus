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
- Keep training code isolated so it can stay optional in low-cost deployments

## Development workflow

- Install dependencies with `uv sync`
- Run the API with `uv run uvicorn knightbus.main:app --reload`
- Run the worker with `uv run python -m knightbus.scheduler`
- Run tests with `uv run pytest`
- Use environment variables from `.env`

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
