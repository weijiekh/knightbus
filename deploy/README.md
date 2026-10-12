# Deploying Knightbus on EC2

> **Legacy.** The production target is now the shared GCP e2-micro. See `deploy/gcp/README.md`. This EC2 path still works with `docker compose --profile web`.

## Architecture

```
Internet
   │  port 80
   ▼
[nginx :80]  ──── /v1/* ────►  [api :56709]
     │                               │
     │  serves index.html            ▼
     │                          [db :5432]
     ▼
  Browser / Phone
```

nginx handles both the frontend and API proxying. Only port 80 (and 22 for SSH) needs to be open.

---

## Step 1 — EC2 Security Group

In **AWS Console → EC2 → Security Groups**, set inbound rules:

| Port | Protocol | Source       | Purpose        |
|------|----------|--------------|----------------|
| 22   | TCP      | Your IP only | SSH            |
| 80   | TCP      | 0.0.0.0/0    | Web + API      |

Port 56709 does **not** need to be open — nginx proxies internally.

---

## Step 2 — Launch the Instance

1. Go to **EC2 → Launch instances**
2. Choose **Amazon Linux 2023** or **Ubuntu 22.04**
3. Select your key pair
4. Attach the security group from Step 1
5. Under **Advanced details → User data**, paste the contents of `deploy/ec2-user-data.sh`
   - Edit `REPO_URL` in the script first to point to your GitHub repo
6. Launch

---

## Step 3 — First-time Setup (SSH in after launch)

```bash
ssh ec2-user@<your-ec2-hostname>   # Amazon Linux
# or
ssh ubuntu@<your-ec2-hostname>     # Ubuntu

cd /opt/knightbus
```

Edit `.env` with your actual config (copied from `.env.example`):

```bash
nano .env
```

Key values to set:
- `GTFS_FEEDS` — leave as-is for Prasarana, or update for your feeds
- `DATABASE_URL` — already set correctly for Docker Compose (`db` hostname)

Start all containers:

```bash
docker compose --profile web up -d
docker compose ps        # verify all 4 containers are running: db, api, worker, web
```

---

## Step 4 — Verify

```bash
# API health
curl http://localhost/v1/health

# Frontend (should return HTML)
curl -s http://localhost | head -5
```

Open on your phone:
```
http://<your-ec2-hostname>
```

---

## Updating

SSH in and run:

```bash
sudo /opt/knightbus/deploy/update.sh
```

This does `git pull` + `docker compose --profile web up --build -d`.

---

## Container overview

| Container | Role | Port (internal) |
|-----------|------|-----------------|
| `db`      | PostgreSQL database | 5432 |
| `api`     | FastAPI + ETA endpoints | 56709 |
| `worker`  | APScheduler — collects GTFS, trains model | — |
| `web`     | nginx — serves frontend, proxies `/v1/*` to api | 80 |

---

## Useful commands

```bash
# View logs
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f web

# Restart a single service
docker compose restart api

# Trigger manual training
docker compose exec worker python scripts/train.py

# Check model versions
docker compose exec db psql -U postgres -d knightbus \
  -c "SELECT version, status, created_at FROM model_versions ORDER BY created_at DESC LIMIT 5;"

# Check recent vehicle data
docker compose exec db psql -U postgres -d knightbus \
  -c "SELECT count(*), max(observed_at) FROM vehicle_states;"
```

---

## HTTPS via Cloudflare Tunnel (free, no domain required)

Browsers block GPS/geolocation on plain HTTP. Cloudflare Tunnel gives you an HTTPS URL instantly, for free, without a domain or SSL cert.

### One-time setup on the EC2 instance

**1. Install `cloudflared`:**

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

**2. Start a quick tunnel (no login needed):**

```bash
cloudflared tunnel --url http://localhost:80
```

Cloudflare will print a URL like `https://random-words.trycloudflare.com`. Open that on your phone — GPS will work.

> The URL changes every time you restart the tunnel. For a permanent URL, follow the steps below.

---

### Permanent tunnel with a fixed URL (requires free Cloudflare account)

**1. Log in:**

```bash
cloudflared tunnel login
```

This opens a browser link — paste it on your local machine, authenticate, and the credentials file is saved to `~/.cloudflared/cert.pem` on the EC2.

**2. Create a named tunnel:**

```bash
cloudflared tunnel create knightbus
```

Note the tunnel ID printed (a UUID like `abc123...`).

**3. Create the config file at `~/.cloudflared/config.yml`:**

```yaml
tunnel: <your-tunnel-id>
credentials-file: /root/.cloudflared/<your-tunnel-id>.json

ingress:
  - hostname: bus.yourdomain.com   # or any subdomain you own on Cloudflare
    service: http://localhost:80
  - service: http_status:404
```

**4. Route DNS (if you have a domain on Cloudflare):**

```bash
cloudflared tunnel route dns knightbus bus.yourdomain.com
```

**5. Run the tunnel:**

```bash
cloudflared tunnel run knightbus
```

**6. Run as a systemd service so it survives reboots:**

```bash
cloudflared service install
systemctl enable cloudflared
systemctl start cloudflared
```

---

### Security group update

When using Cloudflare Tunnel, traffic comes in via the tunnel — not directly on port 80. You can optionally **remove the port 80 inbound rule** from your EC2 security group to prevent direct HTTP access, and only allow the tunnel.

| Port | Protocol | Source       | Purpose        |
|------|----------|--------------|----------------|
| 22   | TCP      | Your IP only | SSH            |
| 80   | TCP      | Remove this  | No longer needed if tunnel-only |

---

## When you get a domain

1. Point your domain's A record to the EC2 public IP
2. Add HTTPS with Certbot:
   ```bash
   sudo apt install certbot python3-certbot-nginx   # Ubuntu
   sudo certbot --nginx -d yourdomain.com
   ```
3. Certbot will update `frontend/nginx.conf` automatically
4. Update the security group to also allow port 443
