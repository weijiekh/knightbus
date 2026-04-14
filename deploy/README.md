# Deploying Knightbus on EC2

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
docker compose up -d
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

This does `git pull` + `docker compose up --build -d`.

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

## When you get a domain

1. Point your domain's A record to the EC2 public IP
2. Add HTTPS with Certbot:
   ```bash
   sudo apt install certbot python3-certbot-nginx   # Ubuntu
   sudo certbot --nginx -d yourdomain.com
   ```
3. Certbot will update `frontend/nginx.conf` automatically
4. Update the security group to also allow port 443
