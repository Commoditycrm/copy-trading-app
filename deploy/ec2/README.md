# EC2 host

Self-hosted Docker stack for prod/QA on EC2 (Ubuntu 24.04, m7i-flex). Replaces
the untracked `docker-compose.override.yml` + `Caddyfile` the Lightsail boxes run.

| File | What |
|---|---|
| `bootstrap.sh` | one-time host setup: patches, 4 GiB swap, Docker with log rotation + capped build cache, hardened sshd, CloudWatch agent package |
| `docker-compose.prod.yml` | db / redis / caddy + memory limits; secrets come from the root `.env` |
| `docker-compose.staging.yml` | pre-cutover overlay: no egress except caddy and Alpaca paper, worker behind the `cutover` profile |
| `systemd/ct-staging-egress-block.service` | the iptables `DOCKER-USER` rules that enforce the overlay's egress policy |
| `Caddyfile` | prod/QA proxy, site from `SITE_ADDRESS` |
| `Caddyfile.staging.example` | staging proxy with basic auth on pages (copy to `Caddyfile.staging`, git-ignored) |
| `cloudwatch-agent.json` | memory, `/` disk and swap only (3 custom metrics, inside the free tier) |
| `env.example` | root `.env` keys |

## Host setup

```bash
sudo bash deploy/ec2/bootstrap.sh
cp deploy/ec2/env.example .env && chmod 600 .env        # fill in
sudo cp deploy/ec2/cloudwatch-agent.json /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
docker compose up -d --build
```

Instance role: `AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`.

## Staging

Keep `docker-compose.staging.yml` in `COMPOSE_FILE` and install the egress unit
before the first `up`:

```bash
sudo cp deploy/ec2/systemd/ct-staging-egress-block.service /etc/systemd/system/
sudo systemctl enable --now ct-staging-egress-block
```

Builds need egress: `systemctl stop` the unit, build, start it again. Never put
the prod `CREDENTIAL_ENCRYPTION_KEY` in staging's `backend/.env` while Lightsail
is live.

## Cutover

1. stop the Lightsail worker + discord-listener and confirm they are down
2. final `pg_dump` → restore here, copy Webull tokens and `caddy_data`
3. prod `backend/.env` (real encryption key), drop the staging overlay from
   `COMPOSE_FILE`, set `COMPOSE_PROFILES=discord`, disable the egress unit
4. `docker compose up -d`, then point DNS at the Elastic IP
