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

COPY . .

EXPOSE 8000

# Self-migrate on startup so a fresh container picks up new alembic revisions
# without a separate deploy step. The CI deploy ALSO runs `alembic upgrade head`
# via `docker compose run --rm backend` before bringing up the live container,
# so this is a belt-and-braces second pass — alembic is idempotent, two
# applications of the same head is a no-op.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
