# Knightbus on the shared GCP e2-micro

Mirrors caruma's `infra/gcp` layout. `db`, `api` and `worker` run in Docker
(`infra/gcp/compose.yaml`, project `knightbus`) on the shared 1 GB VM behind the
host nginx. Memory budget: 448 MB (`db` 128, `api` 192, `worker` 128, each with
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

Repository secrets (used by `train.yml`):

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

First deploy: if the pull from GHCR fails with 403, link the `knightbus-api` and
`knightbus-worker` packages to the repo (GitHub package settings -> Manage
Actions access -> add the repo, role Read).

- `ci.yml` (PRs and pushes to `main`): path-filtered tests. Python tests run when
  `src/**`, `tests/**`, `pyproject.toml` or `uv.lock` change; the Pages Function
  test runs when `frontend/**` changes.
- `deploy.yml` (push to `main`, or manual dispatch which deploys both halves):
  - `web`: `wrangler pages deploy` of `frontend/` when `frontend/**` changed.
  - `image`: builds `ghcr.io/weijiekh/knightbus-api` (`ML_DEPS=inference`) and
    `knightbus-worker` (`ML_DEPS=none`), tagged with the commit SHA and `latest`,
    when `src/**`, `Dockerfile`, `pyproject.toml`, `uv.lock` or `infra/gcp/**`
    changed. Nothing is built on the VM.
  - `api`: SSHes in as `<app-user>`, resets `~/knightbus` to `origin/main`,
    writes the SHA to `API_TAG` in `infra/gcp/.env`, pulls with a job-scoped
    token, runs `up -d --remove-orphans db api worker`, keeps the newest 4 tags
    per knightbus image, then polls `127.0.0.1:8100/v1/health` (dumping
    `knightbus-api` logs on failure).
  - `verify`: fetches `/v1/health` and the SPA root through Pages.
- Manual runs on the VM: `cd ~/knightbus/infra/gcp && docker compose ps`. The
  deployed tag is in `.env`, so a manual `up -d` keeps the same images.
- Rollback: set `API_TAG` in `infra/gcp/.env` to an older SHA still on the box and
  `docker compose up -d api worker`, or re-run the Deploy workflow for that commit.
- Check memory with `docker stats --no-stream` and OOM kills with
  `docker inspect -f '{{.State.OOMKilled}}' knightbus-api`. If `api` hits its
  limit, raise it to 256m and lower `worker`, keeping the 448 MB total.

## 6. Ephemeral IP warning

If the VM is stopped and gets a new external IP, update `DOMAIN`, remove
`/etc/nginx/sites-available/knightbus`, re-run `bootstrap.sh` (new nginx
`server_name` and cert), update the Pages `API_ORIGIN`
and the `DEPLOY_HOST` and `VM_HOST` secrets. Or reserve a static IP.
