# Knightbus on the shared GCP e2-micro

Mirrors caruma's `infra/gcp` layout. `db` and `api` run in Docker
(`infra/gcp/compose.yaml`, project `knightbus`) on the shared 1 GB VM behind the
host nginx. Memory budget: 272 MB (`db` 112, `api` 160; the API process also runs the scheduler, each with
`memswap_limit` equal to `mem_limit`). Host ports: Postgres `127.0.0.1:5433`,
API `127.0.0.1:8100`; nothing is bound on `0.0.0.0`. The frontend lives on
Cloudflare Pages and calls the API through a Pages Function.

Placeholders: `<vm-ip>` is the VM's external IP; `<app-user>` is the SSH account
that owns `~/knightbus` and runs deploys; `knightbus-ci` is the training-only
user created by `bootstrap.sh`.

Never touch caruma's containers, volumes, nginx site or the bare
`<vm-ip>.sslip.io` cert. Knightbus uses `knightbus.<vm-ip>.sslip.io`.
Never run `docker compose down -v`: it deletes the Postgres volume.

## 1. Deploy key, host alias, clone

The repo is private. The VM's `~/.ssh/config` already maps `Host github.com` to
caruma's deploy key, so Knightbus gets its own key and a host alias:

```bash
ssh <app-user>@<vm-ip>
ssh-keygen -t ed25519 -N '' -C knightbus-deploy -f ~/.ssh/knightbus_deploy
cat ~/.ssh/knightbus_deploy.pub   # GitHub: repo Settings -> Deploy keys (read-only)
cat >> ~/.ssh/config <<'CFG'

Host github.com-knightbus
  HostName github.com
  User git
  IdentityFile ~/.ssh/knightbus_deploy
  IdentitiesOnly yes
CFG
git clone git@github.com-knightbus:weijiekh/knightbus.git ~/knightbus
```

`bootstrap.sh` does the key and alias steps itself if they are missing, but it
needs a checkout to run from, so clone first.

## 2. Bootstrap (once, idempotent)

```bash
DOMAIN=knightbus.<vm-ip>.sslip.io SPA_ORIGIN=https://manabas.pages.dev \
  sudo -E bash ~/knightbus/infra/gcp/bootstrap.sh
```

It installs missing packages, writes `infra/gcp/.env` once (generated
`POSTGRES_PASSWORD`, `API_TAG=latest`, GTFS and retention vars from
`.env.example`; never overwritten), starts Postgres, installs the nginx site
`/etc/nginx/sites-enabled/knightbus` (`nginx -t` runs before reload; a failing
site is removed) and gets a cert with certbot unless
`/etc/letsencrypt/live/$DOMAIN` exists. It also creates `knightbus-ci` and
`/opt/knightbus/artifacts` (owned by `knightbus-ci`, mode 755, bind-mounted into
the containers).

`knightbus-ci` is for training only and is NOT in the docker group; the script
removes it from the group if it was added earlier. Add the training public key
to `/home/knightbus-ci/.ssh/authorized_keys` with options that disable
everything except forwarding to the loopback Postgres port:

```
restrict,port-forwarding,permitopen="127.0.0.1:5433" ssh-ed25519 AAAA... github-actions-training
```

It still has a normal shell: the workflow's `scp` and `mv` need command
execution, so a forced `command=` cannot be used.

Firewall and swap are not touched; caruma's bootstrap set them up for this host
(the VM has about 5 GB of swap).

Knightbus Postgres has not been started on the VM yet, so a fresh generated
password is fine. If an interim `/opt/knightbus/.env` exists, copy its
`POSTGRES_PASSWORD` into `infra/gcp/.env` only if a Postgres volume was already
initialised with it; otherwise ignore the interim file and remove it. The interim
`/opt/knightbus` git checkout is not used any more; only `/opt/knightbus/artifacts`
matters.

Set the `DB_PASSWORD` repo secret to the `POSTGRES_PASSWORD` in `infra/gcp/.env`.
`POSTGRES_PASSWORD` only applies when the Postgres volume is first initialised;
changing it later requires `ALTER USER` inside the db.

## 3. GitHub secrets and variables

`production` environment (used by `deploy.yml`):

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_HOST` | secret | `<vm-ip>` |
| `DEPLOY_USER` | secret | `<app-user>` |
| `DEPLOY_SSH_KEY` | secret | private key whose public half is in `<app-user>`'s `authorized_keys` |
| `CLOUDFLARE_API_TOKEN` | secret | token with Pages edit permission |
| `CLOUDFLARE_ACCOUNT_ID` | secret | Cloudflare account id |
| `PAGES_PROJECT` | variable | `manabas` |

Training secrets (used by `train.yml`; set in the `production` environment or
as repository secrets). The training job selects `production`, like deployment,
and validates these secrets before installing dependencies:

| Secret | Value |
|---|---|
| `VM_HOST` | `<vm-ip>` |
| `VM_KNOWN_HOSTS` | output of `ssh-keyscan -t ed25519 <vm-ip>` |
| `VM_USER` | `knightbus-ci` |
| `VM_SSH_KEY` | private key whose public half is in `/home/knightbus-ci/.ssh/authorized_keys` |
| `DB_PASSWORD` | same value as `POSTGRES_PASSWORD` |

The deploy key and the training key are separate: the training key cannot run
docker.

## 4. Cloudflare Pages

Create the `manabas` Pages project as **Direct Upload** (a Git-integrated project cannot be targeted by `wrangler pages deploy`; `deploy.yml` publishes it with
wrangler, no Git integration needed). Set env var
`API_ORIGIN=https://knightbus.<vm-ip>.sslip.io`.

## 5. CI/CD

First deploy: if the pull from GHCR fails with 403, link the `knightbus-api` package to the repo (GitHub package settings -> Manage
Actions access -> add the repo, role Read).

- `ci.yml` (PRs and pushes to `main`): path-filtered tests. Python tests run when
  `src/**`, `tests/**`, `pyproject.toml` or `uv.lock` change; the Pages Function
  test runs when `frontend/**` changes.
- `deploy.yml` (push to `main`, or manual dispatch which deploys both halves):
  - `web`: `wrangler pages deploy` of `frontend/` when `frontend/**` changed.
  - `image`: builds `ghcr.io/weijiekh/knightbus-api` (`ML_DEPS=inference`) and
    (the old `knightbus-worker` image is no longer built), tagged with the commit SHA and `latest`,
    when `src/**`, `Dockerfile`, `pyproject.toml`, `uv.lock` or `infra/gcp/**`
    changed. Nothing is built on the VM.
  - `api`: SSHes in as `<app-user>`, resets `~/knightbus` to `origin/main`,
    writes the SHA to `API_TAG` in `infra/gcp/.env`, pulls with a job-scoped
    token, runs `up -d --remove-orphans db api` (this also removes the old `knightbus-worker` container), keeps the newest 4 tags
    per knightbus image, then polls `127.0.0.1:8100/v1/health` (dumping
    `knightbus-api` logs on failure).
  - `verify`: fetches `/v1/health` and the SPA root through Pages.
- Manual runs on the VM: `cd ~/knightbus/infra/gcp && docker compose ps`. The
  deployed tag is in `.env`, so a manual `up -d` keeps the same images.
- Rollback: set `API_TAG` in `infra/gcp/.env` to an older SHA still on the box and
  `docker compose up -d api`, or re-run the Deploy workflow for that commit.
- Check memory with `docker stats --no-stream` and OOM kills with
  `docker inspect -f '{{.State.OOMKilled}}' knightbus-api`. If `api` hits its
  limit, raise it to 192m, keeping the total under ~300 MB. Measured locally with
  the three KL/MRT/Penang feeds: db ~85 MB, api with the embedded scheduler ~77 MB steady and ~90 MB peak
  during the static reload. Do not run it beside more than
  caruma + krappa on a 1 GB host.

## 6. Ephemeral IP warning

If the VM is stopped and gets a new external IP, update `DOMAIN`, remove
`/etc/nginx/sites-available/knightbus`, re-run `bootstrap.sh` (new nginx
`server_name` and cert), update the Pages `API_ORIGIN`
and the `DEPLOY_HOST` and `VM_HOST` secrets. Or reserve a static IP.

## Optional kiosk GPS supplement

The scheduler can fetch the RapidKL kiosk regional AVL snapshot alongside GTFS.
It uses the existing realtime polling cadence (normally 30 seconds during service,
900 seconds overnight), with one regional request per cycle, not per route/user.
No browser, cookies, websocket client, or extra service is required in production.

Set `KIOSK_AVL_MODE` in `infra/gcp/.env` and recreate the API container to apply:

- `disabled` (default): no kiosk requests; ignore previously stored kiosk positions.
- `shadow`: fetch/validate and log coverage comparisons without storing kiosk positions
  or changing API arrivals. Run for at least one full service day before enabling.
- `supplement`: store validated kiosk GPS alongside GTFS; select each bus's newest
  valid observation by GPS timestamp, preferring GTFS when timestamps are equal.

`KIOSK_AVL_URL` defaults to
`https://myrapidbus.prasarana.com.my/avl/api/avl/region/rkl`. Only configured feeds
named `rapid-bus-kl` and `rapid-bus-mrtfeeder` are eligible for this RKL snapshot.
Shadow logs include `valid`, `mapped`, `additional`, `newer`, and `inserted` counts.
`additional` means no valid, recent GTFS observation was available for that bus;
`newer` means kiosk GPS was newer than a valid GTFS observation. The collectors
remain independent: HTTP failures, bot-check HTML, and malformed snapshots are
logged and do not prevent subsequent polling of either source. There is no
runtime CAPTCHA bypass or retry loop.

Raw kiosk staff identifiers and payloads are not stored. Stored position rows
carry `position_source=kiosk`; vehicle and stop ETA responses expose provenance.
Kiosk rows use the existing realtime retention policy and are excluded from ML
training until validated; kiosk-backed ETAs use schedule progress without ML adjustment. Operational route codes map only to unambiguous static
routes. Unknown trips can appear on the map; ETA progress requires an exact static
trip and an unambiguous stop match within 200m. Route codes alone never determine
direction or fabricate trip IDs. GPS older than two minutes, future GPS more than
30 seconds ahead, nonfinite/out-of-range coordinates, and `(0, 0)` are rejected.

The source is an undocumented kiosk endpoint; its observed availability is not a
service guarantee. Keep GTFS enabled. If the endpoint changes or blocks requests,
set `KIOSK_AVL_MODE=disabled` and recreate the API container. Existing kiosk rows
are immediately excluded from API lookups after restart, without deleting data.
