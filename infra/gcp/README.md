# Knightbus on the shared GCP e2-micro

Knightbus (`db`, `api`, `worker`) runs in Docker on the shared 1 GB VM behind the
host nginx. Memory budget: 450 MB total (`db` 128, `api` 192, `worker` 128, via
`mem_limit`), relying on the existing swap. Host ports: Postgres
`127.0.0.1:5433`, API `127.0.0.1:8100`; nothing is bound on `0.0.0.0`.
The frontend lives on Cloudflare Pages and calls the API through a Pages Function.

Placeholders: `<vm-ip>` is the VM's external IP; `<ci-user>` is the
CI user (training + deploy) created by `bootstrap.sh`.

Never touch other projects' containers, volumes, `/etc/nginx/sites-enabled/cariruma`,
or the `<vm-ip>.sslip.io` cert.

## 1. Pre-checks

```bash
free -m          # confirm >= ~450 MB of RAM + swap headroom
df -h /          # Postgres data budget is 4 GB
docker ps        # note the other projects; leave them alone
```

## 2. Bootstrap (once, idempotent)

```bash
sudo git clone <repo-url> /opt/knightbus   # or let the script clone it
sudo bash /opt/knightbus/deploy/gcp/bootstrap.sh <repo-url>
```

This clones the repo to `/opt/knightbus`, creates `artifacts/` (writable by the
`<ci-user>` user, readable by the api container through the `./artifacts`
bind mount), creates the `<ci-user>` user, installs the nginx site
`/etc/nginx/sites-enabled/knightbus` (`nginx -t` runs before reload) and gets a
cert with `certbot --nginx`. Then add the CI public key to
`/home/<ci-user>/.ssh/authorized_keys`, with key options that disable
everything except forwarding to the loopback Postgres port:

```
restrict,port-forwarding,permitopen="127.0.0.1:5433" ssh-ed25519 AAAA... github-actions-training
```

`<ci-user>` still has a normal shell on this shared VM: the workflow's `scp`
and `mv` need command execution, so a forced `command=` cannot be used. The
options above only stop agent/X11/pty use and limit port forwarding.

## 3. `.env` in `/opt/knightbus`

Generate passwords URL-safe (`openssl rand -hex 24`): they are embedded in
`DATABASE_URL`, and the same value is used for the `DB_PASSWORD` GitHub secret.

```
POSTGRES_PASSWORD=<output of `openssl rand -hex 24`>
DATABASE_URL=postgresql+psycopg://postgres:<same password>@db:5432/knightbus
KNIGHTBUS_DB_PORT=5433
KNIGHTBUS_API_PORT=8100
TRAINING_IN_WORKER=false
REALTIME_RETENTION_DAYS=14        # default 14: prune vehicle position rows older than this
RAW_SNAPSHOT_RETENTION_DAYS=2     # default 2: prune raw feed snapshots older than this
GTFS_FEEDS=...                    # plus the other GTFS_* vars from .env.example
```

## 4. Start

```bash
cd /opt/knightbus
COMPOSE_ARGS="-f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml" deploy/update.sh
curl -s http://127.0.0.1:8100/v1/health
curl -s https://knightbus.<vm-ip>.sslip.io/v1/health
```

`POSTGRES_PASSWORD` only applies when the Postgres volume is first initialised;
changing it later requires `ALTER USER` inside the db.

`update.sh` pulls, rebuilds (`api` with `ML_DEPS=inference`, `worker` with
`ML_DEPS=none`), and starts. Set `PRUNE_BUILD_CACHE=1` to also run `docker builder prune -f`
to protect disk; note it clears the build cache of ALL projects on this shared Docker host.
Check memory with `docker stats --no-stream` and OOM kills with
`docker inspect -f '{{.State.OOMKilled}}' knightbus-api-1`. If `api` hits its
limit, raise it to 256m and lower `worker`, keeping the 450 MB total.

## 5. Cloudflare Pages

Create project `manabas` (fallback `basmana`) via Git integration: root
directory `frontend`, no build command. Set env var
`API_ORIGIN=https://knightbus.<vm-ip>.sslip.io`. Cloudflare builds this
for free, so it uses no GitHub Actions minutes.

## 6. GitHub secrets

Repository secrets (used by `train.yml` and `ci.yml`):

| Secret | Value |
|---|---|
| `VM_HOST` | `<vm-ip>` |
| `VM_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 <vm-ip>` |
| `VM_USER` | `<ci-user>` |
| `VM_SSH_KEY` | private key whose public half is in `/home/<ci-user>/.ssh/authorized_keys` |
| `DB_PASSWORD` | same value as `POSTGRES_PASSWORD` |

The same `VM_SSH_KEY` is used to deploy. `<ci-user>` is in the docker group,
so this key is root-equivalent on the shared VM: keep it only in GitHub
secrets and rotate it if it leaks.

## 7. CI/CD (`.github/workflows/ci.yml`)

- Pull requests: Python tests + Pages Function tests only (~2 billed minutes).
- Push to `main`: tests, then build `ghcr.io/<owner>/knightbus-api` (`ML_DEPS=inference`)
  and `knightbus-worker` (`ML_DEPS=none`) tagged with the commit SHA, then deploy:
  copy the compose files and `deploy/gcp/deploy.sh` to the VM and run it as
  `<ci-user>`. It pulls the SHA-tagged images with a job-scoped token,
  runs `up -d --no-build` and health-checks `127.0.0.1:8100/v1/health`.
  Nothing is built on the VM.
- Changes that touch only `*.md` or `docs/**` skip CI.
- The deployed tags are written to `/opt/knightbus/.images.env`. For manual runs, use
  `docker compose --env-file .env --env-file .images.env -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml ...`
  so that the same images are kept.
- Rollback: re-run the `ci` workflow for an older commit (Actions → ci → that run → Re-run jobs).
- The frontend deploys separately through Cloudflare Pages Git integration (section 5).

## 8. Ephemeral IP warning

If the VM is stopped and gets a new external IP, update the nginx
`server_name`, re-issue the cert, update the Pages `API_ORIGIN`, and update the
`VM_HOST` secret. Or reserve a static IP.
