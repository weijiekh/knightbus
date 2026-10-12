# GCP e2-micro + GitHub Actions Training Rework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Knightbus at near-zero monthly cost: Docker containers (API, worker, Postgres) on the shared free-tier GCP e2-micro `krappa-haus`, ML training moved to a weekly GitHub Actions job, frontend on Cloudflare Pages at `manabas.pages.dev` (fallback `basmana.pages.dev`), then retire EC2.

**Architecture:** All Knightbus services run as Docker Compose containers on `krappa-haus`, next to its existing workloads (`cariruma-api`, `cariruma-db`, `krappa-bot`, `krappa-db`). The host already runs nginx + certbot for `34.123.154.175.sslip.io`. We add one more nginx site, `knightbus.34.123.154.175.sslip.io`, that proxies to the Knightbus API container on `127.0.0.1:8100`. The Cloudflare Pages site serves `frontend/` and has a Pages Function at `/v1/*` that proxies to that HTTPS hostname. The browser therefore stays same-origin: no CORS and no Cloudflare domain needed. A weekly GitHub Actions workflow SSH-tunnels to Knightbus Postgres on `127.0.0.1:5433`, trains, and atomically uploads the model file; the API hot-reloads it.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy, APScheduler, PostgreSQL 16, LightGBM, Docker Compose, host nginx + certbot (existing), GitHub Actions, Cloudflare Pages + Pages Functions.

**Spec:** Decisions agreed in chat on 2026-10-12 (no separate spec doc):
- Lowest monthly cost and lowest ops time. Oracle Free is not possible (billing issue).
- Reuse GCP e2-micro `krappa-haus` (project `krappabot`, `us-central1-a`); it already hosts 2 services.
- All Knightbus services are hosted in Docker.
- Training must not run on the VM. It runs in GitHub Actions **weekly** (Monday ~03:00 MYT) plus manual `workflow_dispatch`.
- Both GitHub repos are private, and the account already uses ~120 min/day, so every Knightbus minute is billed (~$0.008/min).
- Realtime polling stays in the APScheduler worker.
- No Cloudflare domain. Frontend URL: `manabas.pages.dev`, or `basmana.pages.dev` if taken. The backend is reached through sslip.io, the same pattern the existing service uses.

**Server facts (read via `gcloud compute ssh` on 2026-10-12):**
- RAM 969 MB total, **~147 MB available**. Swap 5 GB already configured (190 MB used).
- Disk 30 GB, **8.1 GB free**. Docker uses about 3 GB, of which ~0.5 GB is reclaimable build cache and images.
- Ports already taken: `127.0.0.1:5432` (cariruma-db), `127.0.0.1:8000` (cariruma-api), and `80`/`443` (host nginx).
- Firewall: 22 open to all; 80/443 open for tag `cariruma-web`, which `krappa-haus` has.
- The external IP `34.123.154.175` is ephemeral (`ONE_TO_ONE_NAT`). If it changes, the sslip.io hostnames and the CI host break.

## Global Constraints

- Python 3.12, `uv` for all commands, tests via `uv run pytest`.
- No Redis, Celery, Kafka, Airflow, or Kubernetes (AGENTS.md).
- Every Knightbus service runs in Docker. The only host-level change is one extra nginx site file plus its certbot cert, on the nginx that is already installed.
- Do not touch the existing containers, volumes, `/etc/nginx/sites-enabled/cariruma`, or the `34.123.154.175.sslip.io` cert.
- Knightbus host ports: Postgres `127.0.0.1:5433`, API `127.0.0.1:8100`. Nothing is bound on `0.0.0.0`.
- Knightbus memory budget on the VM: ≤ 450 MB total across `db`, `api`, and `worker` (`mem_limit`), relying on the existing swap.
- Knightbus disk budget: Postgres data ≤ 4 GB.
- Training cron: `"0 19 * * 0"` (Sunday 19:00 UTC = Monday 03:00 MYT) plus `workflow_dispatch`, with `timeout-minutes: 30`.
- Retention defaults: `REALTIME_RETENTION_DAYS=14`, `RAW_SNAPSHOT_RETENTION_DAYS=2`.
- Keep the EC2 path working until cutover (Task 8).
- Commits: conventional (`feat:`, `fix:`, `ci:`, `docs:`), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Model file read while still uploading.** Expect the upload to go to `*.tmp` followed by `mv`. A failed load keeps the previous model. Tested in Task 2.
2. **Corrupt or missing model file.** Expect the API to keep the old model, or fall back to schedule ETAs, and never return a 500. Tested in Task 2.
3. **Retention deletes recent rows.** Expect only rows older than the cutoff to be deleted, and `days <= 0` to disable pruning. Tested in Task 3.
4. **The same vehicle position stored again on every poll.** Expect a repeated `(feed, vehicle_id, timestamp_unix)` to be skipped, while a missing timestamp is still stored. Tested in Task 4.
5. **Backend down or unreachable through the Pages proxy.** Expect `502` with a JSON body, so the frontend shows "API unreachable" instead of hanging. Tested in Task 5.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/knightbus/config.py` | `training_in_worker`, `realtime_retention_days`, `raw_snapshot_retention_days` | 1, 3 |
| `src/knightbus/scheduler.py` | `register_jobs()`, training gate, daily prune job | 1, 3 |
| `src/knightbus/predictor.py` | mtime-based model hot reload | 2 |
| `src/knightbus/etl.py` | `prune_realtime_states()` | 3 |
| `src/knightbus/collector.py` | skip duplicate vehicle positions | 4 |
| `frontend/functions/v1/[[path]].js` | Pages Function proxy to the backend | 5 |
| `frontend/functions/v1/proxy.test.mjs` | Node test for the proxy | 5 |
| `.github/workflows/train.yml` | weekly training | 6 |
| `docker-compose.yml` | env-driven host ports, shared `./artifacts`, `web` behind a profile, restart policies | 7 |
| `deploy/gcp/docker-compose.gcp.yml` | e2-micro overrides: ports, Postgres tuning, mem limits | 7 |
| `deploy/gcp/nginx-knightbus.conf` | host nginx site for `knightbus.34.123.154.175.sslip.io` | 7 |
| `deploy/gcp/bootstrap.sh` | one-time VM setup: clone, artifacts dir, CI user, nginx site, cert | 7 |
| `deploy/gcp/README.md` | GCP, Pages, and secrets runbook | 7 |
| `deploy/update.sh` | accept `COMPOSE_ARGS` | 7 |
| tests | `tests/test_scheduler.py`, `tests/test_predictor.py`, `tests/test_retention.py`, `tests/test_collector_dedup.py` | 1–4 |
| `AGENTS.md`, `README.md`, `.env.example` | docs | 8 |

---

### Task 1: Make worker training optional

**Files:**
- Modify: `src/knightbus/config.py` (after `training_refresh_hours`, ~line 39)
- Modify: `src/knightbus/scheduler.py:104-121`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Settings.training_in_worker: bool` (env `TRAINING_IN_WORKER`, default `True`) and `scheduler.register_jobs(scheduler: BaseScheduler, settings: Settings) -> None`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_scheduler.py`)

```python
from apscheduler.schedulers.background import BackgroundScheduler


def job_names(settings: Settings) -> set[str]:
    sched = BackgroundScheduler(timezone="UTC")
    scheduler.register_jobs(sched, settings)
    return {job.func.__name__ for job in sched.get_jobs()}


def test_training_job_registered_by_default() -> None:
    assert "run_training" in job_names(build_settings())


def test_training_job_skipped_when_disabled() -> None:
    assert "run_training" not in job_names(build_settings(TRAINING_IN_WORKER=False))
```

- [ ] **Step 2:** `uv run pytest tests/test_scheduler.py -v`. Expect FAIL: `module 'knightbus.scheduler' has no attribute 'register_jobs'`.

- [ ] **Step 3: Implement**

`config.py`:
```python
    training_in_worker: bool = Field(default=True, alias="TRAINING_IN_WORKER")
```

`scheduler.py` (add `from apscheduler.schedulers.base import BaseScheduler` and import `Settings` from `knightbus.config`):
```python
def register_jobs(scheduler: BaseScheduler, settings: Settings) -> None:
    scheduler.add_job(
        poll_realtime_active_hours,
        "interval",
        seconds=settings.gtfs_poll_interval_seconds,
        max_instances=1,
    )
    scheduler.add_job(
        poll_realtime_off_hours,
        "interval",
        seconds=settings.gtfs_off_hours_poll_interval_seconds,
        max_instances=1,
    )
    scheduler.add_job(refresh_static, "interval", hours=settings.gtfs_static_refresh_hours, max_instances=1)
    scheduler.add_job(refresh_features, "interval", minutes=settings.feature_refresh_minutes, max_instances=1)
    if settings.training_in_worker:
        scheduler.add_job(run_training, "interval", hours=settings.training_refresh_hours, max_instances=1)
```
In `main()`, replace the five `scheduler.add_job(...)` calls with `register_jobs(scheduler, settings)`.

- [ ] **Step 4:** `uv run pytest -v`. Expect all PASS.
- [ ] **Step 5: Commit** `feat: allow disabling training in worker via TRAINING_IN_WORKER`

---

### Task 2: API hot-reloads the model file

`ETAPredictor` is created once at import (`main.py:16`) and loads the model only then, so a CI-uploaded model would be ignored until a restart.

**Files:**
- Modify: `src/knightbus/predictor.py:14-19` and the `self._ml_model` uses at ~line 76-88
- Test: `tests/test_predictor.py`

**Interfaces:**
- Produces: `ETAPredictor.current_model() -> ResidualModel | None`. It reloads when the file's mtime changes, keeps the previous model if the reload fails, and returns `None` if the file is gone.

- [ ] **Step 1: Write failing tests** (append to `tests/test_predictor.py`)

```python
import os
import pickle

from knightbus import predictor as predictor_module


class StubModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, df):  # noqa: ANN001
        return [self.value]


def write_artifact(path, value: float, mtime: float) -> None:  # noqa: ANN001
    with open(path, "wb") as handle:
        pickle.dump({"model": StubModel(value), "feature_columns": ["route_id"], "categorical_columns": []}, handle)
    os.utime(path, (mtime, mtime))


def make_predictor(monkeypatch, path) -> ETAPredictor:  # noqa: ANN001
    monkeypatch.setenv("MODEL_ARTIFACT_PATH", str(path))
    predictor_module.get_settings.cache_clear()
    try:
        return ETAPredictor()
    finally:
        predictor_module.get_settings.cache_clear()


def test_model_reloads_when_artifact_changes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    assert p.current_model().model.value == 1.0
    write_artifact(path, 2.0, mtime=2000)
    assert p.current_model().model.value == 2.0


def test_corrupt_artifact_keeps_previous_model(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    path.write_bytes(b"not a pickle")
    os.utime(path, (2000, 2000))
    assert p.current_model().model.value == 1.0


def test_missing_artifact_falls_back_to_baseline(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "eta.pkl"
    write_artifact(path, 1.0, mtime=1000)
    p = make_predictor(monkeypatch, path)
    path.unlink()
    assert p.current_model() is None
```

- [ ] **Step 2:** `uv run pytest tests/test_predictor.py -v`. Expect FAIL: `'ETAPredictor' object has no attribute 'current_model'`.

- [ ] **Step 3: Implement** in `predictor.py` (add `from pathlib import Path`):

```python
class ETAPredictor:
    def __init__(self) -> None:
        settings = get_settings()
        self._model_path = Path(settings.model_artifact_path)
        self._model_mtime: float | None = None
        self._ml_model: ResidualModel | None = None
        self._service_tz = settings.gtfs_service_tzinfo
        self.current_model()

    def current_model(self) -> ResidualModel | None:
        try:
            mtime = self._model_path.stat().st_mtime
        except FileNotFoundError:
            self._ml_model = None
            self._model_mtime = None
            return None
        if mtime != self._model_mtime:
            self._model_mtime = mtime
            loaded = load_residual_model(str(self._model_path))
            if loaded is not None:
                self._ml_model = loaded
        return self._ml_model
```
At the top of `get_stop_etas`, add `ml_model = self.current_model()`. Replace `self._ml_model is not None` and `self._ml_model.predict(` with `ml_model is not None` and `ml_model.predict(`. Check whether `get_model_info` or any other method reads `self._ml_model`; if so, switch it to `self.current_model()`.

- [ ] **Step 4:** `uv run pytest -v`. Expect all PASS.
- [ ] **Step 5: Commit** `feat: hot-reload ML artifact in API when file changes`

---

### Task 3: Retention for realtime and raw snapshot tables

Only 8.1 GB of disk is free. Every poll stores a `RawSnapshot` (the full protobuf bytes), and the static GTFS zip is stored daily. Every poll also writes one `VehicleState` row per vehicle, and nothing is ever deleted.

**Files:**
- Modify: `src/knightbus/config.py`, `src/knightbus/etl.py`, `src/knightbus/scheduler.py`
- Create: `tests/test_retention.py`

**Interfaces:**
- Consumes: `register_jobs` from Task 1.
- Produces:
  - `Settings.realtime_retention_days: int` (`REALTIME_RETENTION_DAYS`, default 14)
  - `Settings.raw_snapshot_retention_days: int` (`RAW_SNAPSHOT_RETENTION_DAYS`, default 2)
  - `ETLService.prune_realtime_states(db: Session, realtime_days: int, raw_days: int, now: datetime | None = None) -> int`, which returns the number of rows deleted
  - a daily scheduler job named `prune_realtime`

- [ ] **Step 1: Write failing tests** in `tests/test_retention.py`. Before writing, open `models.py` and confirm the timestamp column names: `VehicleState.observed_at` and `RawSnapshot.fetched_at` are known; check what `TripUpdateState` uses and use that name.

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from knightbus.db import Base
from knightbus.etl import ETLService
from knightbus.models import RawSnapshot, VehicleState

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_prune_removes_only_old_rows() -> None:
    db = build_session()
    db.add_all([
        VehicleState(vehicle_id="old", observed_at=NOW - timedelta(days=15)),
        VehicleState(vehicle_id="new", observed_at=NOW - timedelta(days=13)),
        RawSnapshot(feed_type="vehicle_positions", source_url="x", payload=b"a", fetched_at=NOW - timedelta(days=3)),
        RawSnapshot(feed_type="vehicle_positions", source_url="x", payload=b"b", fetched_at=NOW - timedelta(hours=1)),
    ])
    db.commit()

    deleted = ETLService().prune_realtime_states(db, realtime_days=14, raw_days=2, now=NOW)

    assert deleted == 2
    assert db.execute(select(VehicleState.vehicle_id)).scalars().all() == ["new"]
    assert db.execute(select(RawSnapshot.payload)).scalars().all() == [b"b"]


def test_prune_disabled_when_days_not_positive() -> None:
    db = build_session()
    db.add(VehicleState(vehicle_id="old", observed_at=NOW - timedelta(days=400)))
    db.commit()

    assert ETLService().prune_realtime_states(db, realtime_days=0, raw_days=0, now=NOW) == 0
    assert db.execute(select(func.count()).select_from(VehicleState)).scalar_one() == 1
```
If `RawSnapshot` has other non-null columns, fill them in the test.

Append to `tests/test_scheduler.py`:
```python
def test_prune_job_registered() -> None:
    assert "prune_realtime" in job_names(build_settings())
```

- [ ] **Step 2:** `uv run pytest tests/test_retention.py tests/test_scheduler.py -v`. Expect FAIL: `no attribute 'prune_realtime_states'`.

- [ ] **Step 3: Implement**

`config.py`:
```python
    realtime_retention_days: int = Field(default=14, alias="REALTIME_RETENTION_DAYS")
    raw_snapshot_retention_days: int = Field(default=2, alias="RAW_SNAPSHOT_RETENTION_DAYS")
```

`etl.py`, as a method on `ETLService` (import `delete`, `timedelta`, `RawSnapshot`, `TripUpdateState`, and `VehicleState` as needed):
```python
    def prune_realtime_states(
        self, db: Session, realtime_days: int, raw_days: int, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(timezone.utc)
        targets = []
        if realtime_days > 0:
            cutoff = now - timedelta(days=realtime_days)
            targets += [
                (VehicleState, VehicleState.observed_at < cutoff),
                (TripUpdateState, TripUpdateState.observed_at < cutoff),
            ]
        if raw_days > 0:
            targets.append((RawSnapshot, RawSnapshot.fetched_at < now - timedelta(days=raw_days)))
        deleted = 0
        for model, condition in targets:
            deleted += db.execute(delete(model).where(condition)).rowcount or 0
        db.commit()
        return deleted
```

`scheduler.py`:
```python
def prune_realtime() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        deleted = ETLService().prune_realtime_states(
            db, settings.realtime_retention_days, settings.raw_snapshot_retention_days
        )
        logger.info("pruned realtime states", extra={"deleted": deleted})
```
In `register_jobs`, add `scheduler.add_job(prune_realtime, "interval", hours=24, max_instances=1)`.

- [ ] **Step 4:** `uv run pytest -v`. Expect all PASS.
- [ ] **Step 5: Commit** `feat: prune realtime and raw snapshot rows past retention`

Autovacuum reuses the freed space, so `VACUUM FULL` isn't needed after the first big prune on migrated data (Task 8 handles that once).

---

### Task 4: Skip duplicate vehicle positions

The feed often repeats the same `vehicle.timestamp` across 30-second polls, so duplicate rows waste disk.

**Files:**
- Modify: `src/knightbus/collector.py:170-215` (`normalize_vehicle_positions`)
- Create: `tests/test_collector_dedup.py`

**Interfaces:**
- Produces: `GTFSCollector.latest_vehicle_timestamps(db: Session, feed_name: str, since: datetime) -> dict[str, int]`, which maps `vehicle_id` to the max `timestamp_unix` observed since `since`.

- [ ] **Step 1: Write failing test**

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knightbus.collector import GTFSCollector
from knightbus.db import Base
from knightbus.models import VehicleState

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_latest_vehicle_timestamps() -> None:
    db = build_session()
    db.add_all([
        VehicleState(feed_name="kl", vehicle_id="v1", timestamp_unix=100, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v1", timestamp_unix=130, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v2", timestamp_unix=None, observed_at=NOW),
        VehicleState(feed_name="penang", vehicle_id="v1", timestamp_unix=999, observed_at=NOW),
        VehicleState(feed_name="kl", vehicle_id="v3", timestamp_unix=50, observed_at=NOW - timedelta(hours=2)),
    ])
    db.commit()

    result = GTFSCollector.latest_vehicle_timestamps(db, "kl", since=NOW - timedelta(hours=1))

    assert result == {"v1": 130}
```

- [ ] **Step 2:** `uv run pytest tests/test_collector_dedup.py -v`. Expect FAIL: no attribute `latest_vehicle_timestamps`.

- [ ] **Step 3: Implement** in `collector.py` (import `func` and `timedelta`):

```python
    @staticmethod
    def latest_vehicle_timestamps(db: Session, feed_name: str, since: datetime) -> dict[str, int]:
        rows = db.execute(
            select(VehicleState.vehicle_id, func.max(VehicleState.timestamp_unix))
            .where(
                VehicleState.feed_name == feed_name,
                VehicleState.observed_at >= since,
                VehicleState.timestamp_unix.is_not(None),
            )
            .group_by(VehicleState.vehicle_id)
        ).all()
        return {vehicle_id: ts for vehicle_id, ts in rows}
```
In `normalize_vehicle_positions`, before the loop:
```python
        last_seen = self.latest_vehicle_timestamps(
            db, self.feed.name, since=payload.fetched_at - timedelta(hours=1)
        )
```
Inside the loop, right after `vehicle = entity.vehicle`:
```python
            vehicle_id = vehicle.vehicle.id or entity.id or "unknown"
            timestamp_unix = vehicle.timestamp or None
            if timestamp_unix is not None and last_seen.get(vehicle_id, -1) >= timestamp_unix:
                continue
            if timestamp_unix is not None:
                last_seen[vehicle_id] = timestamp_unix
```
Then use `vehicle_id` and `timestamp_unix` in the `VehicleState(...)` constructor instead of recomputing them.

- [ ] **Step 4:** `uv run pytest -v`. Expect all PASS.
- [ ] **Step 5: Commit** `feat: skip duplicate vehicle positions with unchanged feed timestamp`

---

### Task 5: Pages Function proxy (same-origin API on `*.pages.dev`)

The frontend keeps `API_BASE = ""`. On Pages, `/v1/*` is answered by a Function that forwards to the backend, so `index.html` needs no change and CORS is not needed.

**Files:**
- Create: `frontend/functions/v1/[[path]].js`
- Create: `frontend/functions/v1/proxy.test.mjs`

**Interfaces:**
- Pages environment variable: `API_ORIGIN = https://knightbus.34.123.154.175.sslip.io`.
- Produces: the `onRequest(context)` export.

- [ ] **Step 1: Write failing test** `frontend/functions/v1/proxy.test.mjs` (Node ≥ 20 built-in test runner, no dependencies):

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "./[[path]].js";

const env = { API_ORIGIN: "https://backend.example" };

test("forwards path and query to API_ORIGIN", async (t) => {
  t.mock.method(globalThis, "fetch", async (req) => {
    assert.equal(req.url, "https://backend.example/v1/stops/nearby?lat=3.1&lon=101.6");
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  });
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/stops/nearby?lat=3.1&lon=101.6"), env });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "[]");
});

test("returns 502 JSON when backend unreachable", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("network"); });
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/health"), env });
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { detail: "backend unreachable" });
});

test("rejects non-GET methods", async () => {
  const res = await onRequest({ request: new Request("https://manabas.pages.dev/v1/health", { method: "POST" }), env });
  assert.equal(res.status, 405);
});
```

- [ ] **Step 2:** `node --test frontend/functions/v1/proxy.test.mjs`. Expect FAIL (module not found).

- [ ] **Step 3: Implement** `frontend/functions/v1/[[path]].js`:

```js
// Cloudflare Pages Function: proxy /v1/* to the Knightbus API so the browser stays same-origin.
const TIMEOUT_MS = 10000;

export async function onRequest({ request, env }) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response(JSON.stringify({ detail: "method not allowed" }), {
      status: 405,
      headers: { "content-type": "application/json" },
    });
  }
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, env.API_ORIGIN);
  try {
    const upstream = await fetch(new Request(target.toString(), { method: request.method }), {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return new Response(upstream.body, { status: upstream.status, headers: upstream.headers });
  } catch {
    return new Response(JSON.stringify({ detail: "backend unreachable" }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
}
```
The test's `fetch` mock receives a `Request`. If it doesn't, adjust the test to `String(req.url ?? req)`; don't change the proxy's behaviour.

- [ ] **Step 4:** `node --test frontend/functions/v1/proxy.test.mjs`. Expect 3 PASS.
- [ ] **Step 5:** Update the comment above `API_BASE` at `frontend/index.html:719` to: `// API is same-origin: nginx locally, Pages Function (functions/v1) on Cloudflare Pages`.
- [ ] **Step 6: Commit** `feat: add Cloudflare Pages Function proxy for API`

---

### Task 6: Weekly GitHub Actions training workflow

**Files:**
- Create: `.github/workflows/train.yml`

**Interfaces:**
- Consumes: `python -m knightbus.training` (`training.py:main`, which prints the version), `MODEL_ARTIFACT_PATH`, `DATABASE_URL`.
- Repo secrets:
  - `VM_HOST`: `34.123.154.175`
  - `VM_USER`: `knightbus-ci`
  - `VM_SSH_KEY`: ed25519 private key
  - `VM_KNOWN_HOSTS`: output of `ssh-keyscan -t ed25519 34.123.154.175`
  - `DB_PASSWORD`

- [ ] **Step 1: Write the workflow**

```yaml
name: train

on:
  schedule:
    - cron: "0 19 * * 0" # Monday 03:00 Asia/Kuala_Lumpur
  workflow_dispatch:

concurrency:
  group: train
  cancel-in-progress: false

jobs:
  train:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.12"
          enable-cache: true

      - name: Install
        run: uv sync --extra ml --frozen

      - name: Configure SSH
        run: |
          set -euo pipefail
          mkdir -p ~/.ssh
          echo "${{ secrets.VM_SSH_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          echo "${{ secrets.VM_KNOWN_HOSTS }}" > ~/.ssh/known_hosts

      - name: Open DB tunnel
        run: |
          set -euo pipefail
          ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
            -L 15432:127.0.0.1:5433 "${{ secrets.VM_USER }}@${{ secrets.VM_HOST }}"
          for i in $(seq 1 10); do nc -z 127.0.0.1 15432 && exit 0; sleep 1; done
          echo "tunnel did not come up" >&2; exit 1

      - name: Train
        env:
          DATABASE_URL: postgresql+psycopg://postgres:${{ secrets.DB_PASSWORD }}@127.0.0.1:15432/knightbus
          MODEL_ARTIFACT_PATH: artifacts/eta_lgbm.pkl
        run: uv run python -m knightbus.training

      - name: Upload artifact to VM
        run: |
          set -euo pipefail
          if [ ! -s artifacts/eta_lgbm.pkl ]; then
            echo "No model produced (insufficient/stale data); live model unchanged."
            exit 0
          fi
          scp artifacts/eta_lgbm.pkl "${{ secrets.VM_USER }}@${{ secrets.VM_HOST }}:/opt/knightbus/artifacts/eta_lgbm.pkl.tmp"
          ssh "${{ secrets.VM_USER }}@${{ secrets.VM_HOST }}" \
            "mv /opt/knightbus/artifacts/eta_lgbm.pkl.tmp /opt/knightbus/artifacts/eta_lgbm.pkl"
```

Notes:
- Run `uv sync --extra ml --frozen` locally first. If the lock is stale, run `uv lock` and commit `uv.lock`.
- Training writes its `ModelVersion` row through the tunnel, so model history stays in the VM's database.
- `insufficient_data` and `stale_data` results exit 0 and skip the upload. That is intended.
- Pulling 200k rows across regions takes a few minutes at most; check the run time in Task 8.

- [ ] **Step 2:** `docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:latest`. Expect no errors.
- [ ] **Step 3: Commit** `ci: add weekly LightGBM training workflow over SSH tunnel`

---

### Task 7: Docker Compose for the shared e2-micro, plus host nginx site

**Files:**
- Modify: `docker-compose.yml`, `deploy/update.sh`
- Create: `deploy/gcp/docker-compose.gcp.yml`, `deploy/gcp/nginx-knightbus.conf`, `deploy/gcp/bootstrap.sh`, `deploy/gcp/README.md`

- [ ] **Step 1: `docker-compose.yml`**
  - Add top-level `name: knightbus` so container and volume names never collide with the other projects on the VM.
  - `db.environment.POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}`.
  - `db.ports: ["127.0.0.1:${KNIGHTBUS_DB_PORT:-5432}:5432"]`. This also fixes the current setup, which publishes Postgres on all interfaces.
  - `api.ports: ["127.0.0.1:${KNIGHTBUS_API_PORT:-56709}:56709"]`.
  - `api` and `worker`: `volumes: ["./artifacts:/app/artifacts"]`. Today the two containers don't share models.
  - `web`: `profiles: ["web"]`, so EC2 and local runs use `--profile web` and GCP skips it.
  - `db`, `api`, `worker`: `restart: unless-stopped`.

- [ ] **Step 2: `deploy/gcp/docker-compose.gcp.yml`**

```yaml
# Shared e2-micro (krappa-haus). Usage:
#   docker compose -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml up -d --build
services:
  db:
    command:
      - postgres
      - -c
      - shared_buffers=32MB
      - -c
      - work_mem=2MB
      - -c
      - maintenance_work_mem=32MB
      - -c
      - max_connections=15
      - -c
      - effective_cache_size=128MB
    mem_limit: 128m

  api:
    mem_limit: 192m

  worker:
    environment:
      TRAINING_IN_WORKER: "false"
    mem_limit: 160m
```
Set `KNIGHTBUS_DB_PORT=5433` and `KNIGHTBUS_API_PORT=8100` in the VM's `.env`; Compose reads `.env` for interpolation.

- [ ] **Step 3: `deploy/gcp/nginx-knightbus.conf`**

```nginx
# Host nginx site (krappa-haus). certbot --nginx adds the 443 block.
server {
    listen 80;
    listen [::]:80;
    server_name knightbus.34.123.154.175.sslip.io;

    location /v1/ {
        proxy_pass         http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }

    location / {
        return 404;
    }
}
```

- [ ] **Step 4: `deploy/gcp/bootstrap.sh`** (run once on the VM with `sudo bash deploy/gcp/bootstrap.sh <repo-url>`)

```bash
#!/bin/bash
# One-time Knightbus setup on the shared GCP e2-micro. Idempotent.
# Does not touch other projects' containers, nginx sites, or certs.
set -euo pipefail

APP_DIR=/opt/knightbus
REPO_URL="${1:?usage: bootstrap.sh <git-repo-url>}"
API_HOST=knightbus.34.123.154.175.sslip.io

command -v docker >/dev/null || { echo "docker missing" >&2; exit 1; }

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/artifacts"

# Restricted CI user: SSH port-forward to loopback Postgres + write model artifacts.
if ! id knightbus-ci >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash knightbus-ci
fi
chown -R knightbus-ci:knightbus-ci "$APP_DIR/artifacts"
install -d -m 700 -o knightbus-ci -g knightbus-ci /home/knightbus-ci/.ssh

if [ ! -e /etc/nginx/sites-enabled/knightbus ]; then
  cp "$APP_DIR/deploy/gcp/nginx-knightbus.conf" /etc/nginx/sites-available/knightbus
  ln -s /etc/nginx/sites-available/knightbus /etc/nginx/sites-enabled/knightbus
  nginx -t
  systemctl reload nginx
  certbot --nginx -d "$API_HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect
fi

echo "Next: add CI public key to /home/knightbus-ci/.ssh/authorized_keys, create $APP_DIR/.env, then run deploy/update.sh"
```
If `nginx -t` fails, the script stops before reloading, so the other site is unaffected.

- [ ] **Step 5: `deploy/update.sh`.** Read the compose arguments from the environment:

```bash
COMPOSE_ARGS="${COMPOSE_ARGS:--f docker-compose.yml --profile web}"
...
docker compose $COMPOSE_ARGS up --build -d
docker compose $COMPOSE_ARGS ps
```
On GCP: `COMPOSE_ARGS="-f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml" deploy/update.sh`. After the build, run `docker builder prune -f` there to protect disk.

- [ ] **Step 6: Local verification**
  - `docker compose -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml config`. Expect valid output, no `web`, and mem limits set.
  - `docker compose --profile web up --build -d` then `curl -s localhost/v1/health`. Expect healthy JSON (EC2 shape still works).
  - `KNIGHTBUS_API_PORT=8100 docker compose -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml up --build -d` then `curl -s localhost:8100/v1/health`. Expect healthy JSON.
  - `docker stats --no-stream`. Expect db + api + worker ≤ 450 MB, with no container OOM-killed (`docker inspect -f '{{.State.OOMKilled}}'`). If the API exceeds its limit because of the pandas/lightgbm imports, raise `api` to 256m and lower `worker`, keeping the 450 MB total.
  - `docker compose down`.

- [ ] **Step 7: `deploy/gcp/README.md`** runbook, in order:
  1. Pre-checks (`free -m`, `df -h /`, `docker ps`).
  2. Bootstrap.
  3. `.env`: `POSTGRES_PASSWORD`, `DATABASE_URL=postgresql+psycopg://postgres:<pw>@db:5432/knightbus`, `KNIGHTBUS_DB_PORT=5433`, `KNIGHTBUS_API_PORT=8100`, `TRAINING_IN_WORKER=false`, retention vars, plus the existing GTFS vars.
  4. Start with `update.sh`.
  5. Cloudflare Pages: create project `manabas` (fallback `basmana`) via Git integration, root dir `frontend`, no build command, env var `API_ORIGIN`. Cloudflare builds this for free, so it uses no GitHub Actions minutes.
  6. GitHub secrets (Task 6).
  7. Ephemeral IP warning: if the VM is stopped and gets a new IP, update the nginx `server_name`, the cert, `API_ORIGIN`, and `VM_HOST`, or reserve a static IP.

- [ ] **Step 8: Commit** `feat: add Docker deploy for shared GCP e2-micro behind host nginx`

---

### Task 8: Cutover and docs (operator task, done together with the user)

- [ ] **Step 1: Measure the EC2 data size.**
  - `docker compose exec db psql -U postgres -d knightbus -c "select relname, pg_size_pretty(pg_total_relation_size(relid)) from pg_catalog.pg_statio_user_tables order by pg_total_relation_size(relid) desc limit 8;"`
  - If keeping 14 days of `vehicle_states` would be more than ~3 GB, lower `REALTIME_RETENTION_DAYS` (training needs ≥ 7 days).
- [ ] **Step 2: Migrate only what's needed.**
  - Static GTFS tables are reloaded by the worker at startup, so they don't need copying.
  - Copy `vehicle_states`, `trip_update_states`, and `model_versions` for the last N days:
    - On EC2: `\copy (select * from vehicle_states where observed_at > now() - interval '14 days') to 'vs.csv' csv header`, and the same for the others.
    - `scp` the files to the VM.
    - `\copy ... from` into the new database after the worker has created the tables.
    - Run `select setval(...)` on the id sequences.
  - Skip `raw_snapshots`.
- [ ] **Step 3: Start the services.**
  - Bootstrap, `.env`, `update.sh`.
  - `curl https://knightbus.34.123.154.175.sslip.io/v1/health`.
  - `docker stats` and `free -m`: confirm the other services are still healthy (`docker ps` shows `cariruma-api` as healthy).
- [ ] **Step 4: Train.** Add the GitHub secrets and run `train` via `workflow_dispatch`. Expect the version printed, the model uploaded, and the API's model-info endpoint showing the new version. Check the billed minutes (expect ≤ 10).
- [ ] **Step 5: Pages.** Deploy, then open `https://manabas.pages.dev` on a phone and check that geolocation and ETAs work.
- [ ] **Step 6: Watch for 48 hours.** Check `free -m`, swap use, `df -h /`, and worker logs. Then stop the EC2 instance; after 1 week, terminate it and delete its EIP and EBS volume.
- [ ] **Step 7: Docs.**
  - `AGENTS.md`: deploy target is the GCP e2-micro (shared), training runs from `.github/workflows/train.yml` weekly, and document the new env vars and the Pages proxy.
  - `README.md`: update "Default deployment shape".
  - `.env.example`: add the new vars.
  - Mark `deploy/README.md` as legacy EC2.
  - Commit `docs: document GCP + GitHub Actions training deployment`.

---

## Open Questions

1. Is the external IP billed in your GCP account (~$3.6/mo)? Check Billing → Reports filtered to "External IP". If it is, reserving a static IP costs the same, so reserve it; that removes the ephemeral-IP risk.
2. Let's Encrypt limits: sslip.io is shared by many users. If `certbot` hits a rate limit for `knightbus.34.123.154.175.sslip.io`, the fallback is to add a `location /knightbus/v1/` block to the existing cariruma server block, which needs no new cert. That requires your OK, since it edits the other project's nginx file.

---

### Task 9: Memory optimization: lean inference image and model format

**Execution order:** run after Task 2 and before Task 6 (Task 6's workflow must produce the new model format).

**Why:** the e2-micro has about 147 MB free. Today the API image installs pandas and scikit-learn, and unpickling an `LGBMRegressor` imports both. `import lightgbm` also imports pandas and sklearn whenever they are installed (`lightgbm/compat.py`). The API only needs `lightgbm` + `numpy`. The worker on GCP needs no ML at all (`TRAINING_IN_WORKER=false`).

**Files:**
- Modify: `src/knightbus/ml.py` (artifact format + numpy-only predict)
- Modify: `src/knightbus/training.py` (call `save_residual_model` with the fitted model; the format change lives in `ml.py`)
- Modify: `Dockerfile` (build arg `ML_DEPS`)
- Modify: `deploy/gcp/docker-compose.gcp.yml` if it exists by then; otherwise add the same keys in Task 7 (build arg `ML_DEPS: inference`, `MALLOC_ARENA_MAX: "2"` for `api` and `worker`)
- Modify: `tests/test_predictor.py` (Task 2's hot-reload tests used pickled stub dicts; switch them to monkeypatching `predictor_module.load_residual_model` so they don't depend on the artifact format)
- Create: `tests/test_ml.py`

**Interfaces:**
- `save_residual_model(path, model, feature_columns, categorical_columns, metrics)` keeps its signature. It now pickles `{"format": "lgbm-booster-v1", "model_str": model.booster_.model_to_string(), "feature_columns": [...], "categorical_columns": [...], "categories": {col: [values...]}, "metrics": metrics}`. `categories` comes from `model.booster_.pandas_categorical`, zipped with the categorical columns in `feature_columns` order.
- `load_residual_model(path) -> ResidualModel | None` builds `lightgbm.Booster(model_str=...)`. It returns `None` (and logs) for the old format or a corrupt file.
- `ResidualModel.predict(features: dict[str, object]) -> float` uses numpy only. A categorical value maps to its index in `categories[col]`; an unseen value or `None` maps to `np.nan` (missing). A numeric value is converted with `float()` and falls back to `0.0`, matching training's `fillna(0)`.

- [ ] **Step 1: Write failing test** `tests/test_ml.py`

```python
import pytest

lgb = pytest.importorskip("lightgbm")
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from knightbus.ml import load_residual_model, save_residual_model


def fit_model():
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({
        "route_id": rng.choice(["r1", "r2", "r3"], n),
        "hour": rng.integers(0, 24, n).astype(float),
    })
    df["route_id"] = df["route_id"].astype("category")
    y = (df["route_id"].cat.codes * 30 + df["hour"]).astype(float)
    model = lgb.LGBMRegressor(n_estimators=20, min_child_samples=5, verbose=-1)
    model.fit(df, y, categorical_feature=["route_id"])
    return model, df


def test_numpy_predict_matches_sklearn_predict(tmp_path) -> None:
    model, df = fit_model()
    path = tmp_path / "m.pkl"
    save_residual_model(str(path), model, ["route_id", "hour"], ["route_id"], metrics={})

    loaded = load_residual_model(str(path))

    assert loaded is not None
    for route, hour in [("r1", 3.0), ("r3", 20.0)]:
        expected = model.predict(pd.DataFrame({"route_id": pd.Categorical([route], categories=df["route_id"].cat.categories), "hour": [hour]}))[0]
        assert loaded.predict({"route_id": route, "hour": hour}) == pytest.approx(expected, rel=1e-6)


def test_unseen_category_and_missing_numeric_do_not_crash(tmp_path) -> None:
    model, _ = fit_model()
    path = tmp_path / "m.pkl"
    save_residual_model(str(path), model, ["route_id", "hour"], ["route_id"], metrics={})
    loaded = load_residual_model(str(path))
    assert isinstance(loaded.predict({"route_id": "never-seen", "hour": None}), float)


def test_old_pickle_format_returns_none(tmp_path) -> None:
    import pickle
    path = tmp_path / "old.pkl"
    path.write_bytes(pickle.dumps({"model": object(), "feature_columns": ["a"]}))
    assert load_residual_model(str(path)) is None
```

- [ ] **Step 2:** `uv sync --extra ml && uv run pytest tests/test_ml.py -v`. Expect FAIL.

- [ ] **Step 3: Implement** the `ml.py` changes per Interfaces. Keep `import numpy as np` and `import lightgbm as lgb` lazy (inside functions) so the API still starts without ML deps, and returns `None` with a warning if they are missing. Remove the pandas import from `predict`. Build the row as `np.array([[...]], dtype=float)` in `feature_columns` order and call `self.booster.predict(row)[0]`. Rename the dataclass field `model` to `booster`, and update `predictor.py` usages if any reference `.model`. In `tests/test_predictor.py`, change Task 2's tests to monkeypatch `predictor_module.load_residual_model` (returning a sentinel object per call), keeping the same three behaviours: reload on mtime change, keep the previous model on failure, `None` when the file is missing.

- [ ] **Step 4: Dockerfile build arg**

```dockerfile
ARG ML_DEPS=full
RUN uv pip install --system . \
    && if [ "$ML_DEPS" = "full" ]; then \
         uv pip install --system "lightgbm>=4.5.0" "numpy>=2.1.0" "pandas>=2.2.0" "scikit-learn>=1.5.0"; \
       elif [ "$ML_DEPS" = "inference" ]; then \
         uv pip install --system "lightgbm>=4.5.0" "numpy>=2.1.0"; \
       fi
```
(This replaces the existing single `RUN uv pip install ...` line. `ML_DEPS=none` installs nothing extra.)

In the GCP override, set `api: build: {context: ., args: {ML_DEPS: inference}}`, `worker: build: {context: ., args: {ML_DEPS: none}}`, and `environment: MALLOC_ARENA_MAX: "2"` on both. Both images then differ, so set distinct `image:` names (`knightbus-api:local`, `knightbus-worker:local`) to avoid one build overwriting the other.

- [ ] **Step 5:** `uv run pytest -v`. Expect all PASS.

- [ ] **Step 6: Measure.** Build the inference image and record peak RSS before and after:
  - `docker build --build-arg ML_DEPS=inference -t kb-inf .`
  - `docker build -t kb-full .`
  - For each image: `docker run --rm -e DATABASE_URL=sqlite:// <img> python -c "import resource, knightbus.main, knightbus.ml as m, lightgbm; print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024, 'MB')"`
  - Also confirm that `python -c "import lightgbm, sys; print('pandas' in sys.modules, 'sklearn' in sys.modules)"` prints `False False` in `kb-inf`.

  Write both numbers in the report. Expect the inference image to be noticeably lower; if not, report that instead of forcing it.

- [ ] **Step 7: Commits (atomic):**
  - `feat: store LightGBM booster string and predict without pandas`
  - `build: add ML_DEPS build arg for lean inference and worker images`

---

### Task 10: Stream static GTFS ingestion (memory)

**Why:** Task 7 measured that the worker is OOM-killed during `load_static_feed` at 128m, 192m and 256m limits (peak ~200+ MiB; 384m survives), while it idles at ~58 MiB. `refresh_static` runs at startup and then every `GTFS_STATIC_REFRESH_HOURS` (24h), so this spike recurs daily. `collector.py:68-168` reads every CSV fully into `list[dict]`, keeps every ORM `Stop`/`Trip` object in lookup dicts, and adds every `StopTime` as an ORM object to the session before one commit.

**Target:** first and daily static ingest of every configured feed completes with the worker at `mem_limit: 128m` (Task 7 value), with no change to the stored data.

**Files:**
- Modify: `src/knightbus/collector.py` (`load_static_feed`, `_read_csv`, `_read_csv_optional`)
- Create: `tests/test_static_ingest.py`

**Interfaces:**
- `load_static_feed(self, db: Session, payload: FeedPayload) -> None` keeps its signature and resulting rows: the same tables, columns and per-feed delete-then-insert semantics.
- Add `STATIC_INSERT_BATCH_SIZE = 5000` (module constant).

- [ ] **Step 1: Write a characterization test FIRST, against the current code.** Build a tiny in-memory GTFS zip with `routes.txt`, `stops.txt`, `trips.txt`, `stop_times.txt` (including one row whose trip_id or stop_id is unknown, so it is skipped), `calendar.txt` and `calendar_dates.txt`. Run `load_static_feed` on SQLite in memory and assert:
  - exact row counts per table;
  - `StopTime.trip_id_ref` and `stop_id_ref` point at the right `Trip` and `Stop` (join on ids);
  - `arrival_seconds` is parsed;
  - a second load of the same payload replaces rather than duplicates the rows;
  - rows of another `feed_name` are untouched.

  Run it and expect PASS on the current code. Commit `test: characterize static GTFS ingestion`.
- [ ] **Step 2: Implement streaming.**
  - Read each CSV with `csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))` and iterate it, never materializing a list. Keep the existing behaviour for missing optional files.
  - Insert routes, stops, trips, calendar and calendar_dates with SQLAlchemy Core `insert(Model)` in batches of `STATIC_INSERT_BATCH_SIZE`.
  - Then build `stop_ids: dict[str, int]` and `trip_ids: dict[str, int]` with `select(Stop.stop_id, Stop.id).where(Stop.feed_name == ...)` (and the same for trips). These maps hold plain strings and ints, not ORM objects.
  - Stream `stop_times.txt`, insert in Core batches, and skip rows with unknown trip or stop as today.
  - Keep the zip open only while streaming. The `payload.payload` bytes are already in memory, so don't copy them.
  - Commit once at the end, as today. Bulk Core inserts don't accumulate identity-map objects.
- [ ] **Step 3:** Run the Step 1 test unchanged and expect PASS, then the full suite with `uv run pytest`. Commit `perf: stream static GTFS ingestion with batched core inserts`.
- [ ] **Step 4: Measure.** Using `docker-compose -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml` with worker `mem_limit: 128m`:
  - start db and worker on a fresh volume;
  - confirm 0 worker restarts (`docker inspect -f '{{.RestartCount}}'`) through the first static load of all feeds;
  - record peak RSS from `docker stats` sampling.

  If the upstream returns 429, retry later or feed a locally downloaded zip through a one-off script, and say which one you used. Then `down` and remove only the volume you created.
