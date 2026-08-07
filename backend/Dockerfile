FROM python:3.11-slim

WORKDIR /app

# System deps needed to build psycopg + a few Python wheels with C extensions.
# build-essential is dropped from the final image by `rm -rf /var/lib/apt/lists/*`
# being on the same RUN layer — but the compilers themselves stay in /usr, which
# we accept for now (smallness vs. simplicity). Trim to `slim` distroless later
# if image size becomes a problem.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Webull's official SDK is installed --no-deps on purpose: its metadata caps
# `cryptography<43` on Python <3.14, which conflicts with our CVE-pinned
# cryptography==44.0.1. Its actual runtime deps (grpcio/protobuf/jmespath/
# cachetools/paho-mqtt/six) are pinned in requirements.txt above and already
# installed, so --no-deps just adds the SDK itself. See the note in
# requirements.txt. The SDK only uses cryptography for stable HMAC/RSA signing,
# so running it against cryptography 44 is safe.
RUN pip install --no-cache-dir --no-deps webull-openapi-python-sdk==2.0.16

COPY . .

# Run as a non-root user so a compromised process can't modify the image,
# install packages, or escalate. App files copied above are world-readable;
# all runtime writes go to the noexec tmpfs /tmp (HOME=/tmp in compose). Pairs
# with read_only + cap_drop + no-new-privileges in docker-compose.yml.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
# Durable, writable dir for the Webull OpenAPI token. A named volume is mounted
# here in docker-compose so the VERIFIED token survives restarts and is shared
# by backend + worker — so the trader confirms the Open-API 2FA ONCE, not on
# every restart. Pre-created with appuser ownership so, under the read-only
# rootfs + non-root user, the SDK can write it (Docker copies this ownership
# onto the fresh named volume on first mount). Set WEBULL_OPENAPI_TOKEN_DIR to
# /data/webull_token in backend/.env to use it.
RUN mkdir -p /data/webull_token && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

# Self-migrate on startup so a fresh container picks up new alembic revisions
# without a separate deploy step. The CI deploy ALSO runs `alembic upgrade head`
# via `docker compose run --rm backend` before bringing up the live container,
# so this is a belt-and-braces second pass — alembic is idempotent, two
# applications of the same head is a no-op.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
