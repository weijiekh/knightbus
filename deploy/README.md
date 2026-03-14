# Deploying Knightbus on EC2

## Security Group (inbound rules)

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | Your IP only | SSH |
| 56709 | TCP | Your IP / VPC | Knightbus API |

Add 80/443 if you put a reverse proxy (nginx/Caddy/ALB) in front later.

## Launch Steps

1. **Launch an EC2 instance** (Amazon Linux 2023 or Ubuntu 22.04)
   - Attach a security group with the rules above
   - Attach an IAM role if using Parameter Store for secrets

2. **Edit the user data script** before launching:
   - Set `REPO_URL` to your actual GitHub repo URL
   - After launch, SSH in and edit `.env` with real credentials (copied from `.env.example`)
   - Or use Parameter Store — see comments in the script

3. **Paste `ec2-user-data.sh` as User Data** in the EC2 launch wizard (Advanced Details section)

4. **Launch** — the instance will bootstrap itself. SSH in after a few minutes to verify:
   ```bash
   ssh ec2-user@<ip>          # Amazon Linux
   ssh ubuntu@<ip>            # Ubuntu
   cd /opt/knightbus
   docker compose ps
   docker compose logs -f
   ```

## Updating

SSH in and run:

```bash
sudo /opt/knightbus/deploy/update.sh
```

Or pass a custom path:

```bash
sudo /opt/knightbus/deploy/update.sh /opt/knightbus
```

## Container Auto-Restart

All services in `docker-compose.yml` use `restart: unless-stopped`, so containers come back automatically after a reboot.
