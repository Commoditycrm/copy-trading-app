# Security hardening — what we learned from the May 2026 incident

**Incident:** AWS abuse case 16220881880. Lightsail instance `Node-js-1` (static IP `3.149.150.222`) was implicated in scanning remote hosts on port 9200 (Elasticsearch). AWS Trust & Safety blackholed outbound traffic. Restoring from snapshots carried the compromise forward.

**Root cause:** Postgres (5432) and Redis (6379) were bound to `0.0.0.0` and exposed to the public internet via Lightsail's default-open firewall. Redis had no password. An attacker exploited the unauthed Redis to drop a port-scanner that then targeted other Elasticsearch instances.

This doc captures the lessons so the same hole doesn't re-open.

---

## Network exposure: only what's needed

### Bind databases and caches to localhost

`docker-compose.yml`:

```yaml
db:
  ports:
    - "127.0.0.1:5432:5432"     # NOT "5432:5432"
redis:
  ports:
    - "127.0.0.1:6379:6379"     # NOT "6379:6379"
```

The containers can still talk to each other via docker compose's internal network using service names (`db:5432`, `redis:6379`) — the `ports:` directive is only for host-to-container exposure. Localhost-only binding means a remote attacker has no path to these services.

For most apps, you can **remove the `ports:` block entirely** since nothing outside the docker network needs direct access. Only keep the binding if you SSH-tunnel into the host for ops.

### Auth on every service

```yaml
redis:
  command: ["redis-server", "--requirepass", "${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}"]
```

The `:?` syntax fails docker compose startup if the env var is missing — better to refuse to boot than to start unauthed.

Backend's `REDIS_URL` then becomes:

```
REDIS_URL=redis://:<password>@redis:6379/0
```

### Lightsail firewall: deny by default, allow what you need

Lightsail console → instance → **Networking** → **IPv4 Firewall**.

Keep only:

| Port | Why | Restricted to |
|---|---|---|
| 22 (SSH) | Admin access | **Your IP only** — NOT "Any IPv4 address" |
| 80 (HTTP) | Caddy reverse proxy | Any (public web traffic) |
| 443 (HTTPS) | Caddy TLS | Any (public web traffic) |

Delete every other rule, especially anything that opened 5432, 6379, 3000, 8000.

---

## Operating system: minimal blueprint

The Bitnami / Lightsail "Node.js" blueprint we used originally ships with:

- A sample Node.js app on port 80 (we found it as `nodeapp` in PM2)
- A system-installed Redis on 6379

Both are running by default. The system Redis competed with our docker redis (we disabled it via `systemctl`), and the sample Node app is an unaudited attack surface.

**Use the plain Ubuntu blueprint instead.** Install only Docker and git. Nothing else.

```bash
# On a fresh Ubuntu instance
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
# log out + back in for group to take effect
```

That's it. No PM2, no system Redis, no sample apps.

---

## Static IP and account state after an abuse flag

If an IP gets blackholed by Trust & Safety:

1. **Release the static IP** from your account. Don't reuse it — it's flagged in AWS's abuse systems and may get the same treatment again.
2. **Allocate a fresh static IP** when creating the replacement instance.
3. **Do not restore from snapshots taken after the compromise window** — they carry the malware. Any snapshot post-incident is potentially infected.

---

## After a compromise: rotate every credential

Assume the attacker read everything in memory and on disk:

- All broker API keys (Alpaca, SnapTrade, Webull) for trader AND every subscriber — every key needs regenerating from the broker's dashboard
- `JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` from `backend/.env` (regenerate with `openssl rand -base64 48`)
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `SNAPTRADE_CONSUMER_KEY` (rotate in SnapTrade dashboard)
- AWS account console password + enable MFA

User passwords are bcrypt-hashed in the DB so the attacker doesn't have plaintext, but:

- Force a password reset on the trader's account
- Force resets on any subscriber accounts that had real value (broker connections)

---

## Monitoring and early warning

The abuse case fired AFTER the scanning had already started. We could have caught it earlier with:

- **Lightsail CloudWatch alarms** on outbound network traffic. Set a threshold (e.g. > 100 MB/h outbound) — anything that scans the internet ramps quickly.
- **Email notifications** from the AWS account itself — sometimes filtered to spam. The abuse email is critical.
- **Uptime monitoring** (UptimeRobot, free) on `https://your-domain/api/health` — catches the moment outbound dies.
- **Deploy failure notifications** in GitHub Actions — silent CI failures masked the real outage by 12+ hours.

---

## Quick incident-response checklist (for next time)

If your IP gets blackholed:

1. Read the abuse email carefully — note the destination port being scanned (gives you a hint about which exploit ran)
2. Don't restart, don't restore from snapshot — assume the disk is infected
3. Reply to AWS within 24h with a detailed plan, or your account may be suspended
4. Terminate compromised instances; release flagged IPs
5. Build fresh from Ubuntu blueprint + git clone (not snapshot)
6. Apply this doc's hardening before opening any port
7. Rotate every secret
8. Notify users if their broker credentials passed through the compromised instance

---

## Reference

- AWS Shared Responsibility Model: https://aws.amazon.com/compliance/shared-responsibility-model/
- Redis security best practices: https://redis.io/docs/management/security/
- Lightsail firewall docs: https://lightsail.aws.amazon.com/ls/docs/en_us/articles/understanding-firewall-and-port-mappings-in-amazon-lightsail
