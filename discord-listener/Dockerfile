# Playwright's own image: Chromium plus its system libraries, built and tested
# against the matching Python package. Installing browsers into a plain slim
# image means chasing a long list of shared-object dependencies by hand.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Chromium executes from /ms-playwright in the image layer, which stays
# executable under a read-only root filesystem — so this service keeps the same
# hardening as every other container (read_only, noexec /tmp, cap_drop ALL,
# no-new-privileges). See docker-compose.yml. Chromium's own sandbox is disabled
# instead (--no-sandbox in runner.py) because it needs capabilities we drop; the
# container boundary is the isolation, and this service holds no database or
# broker credentials to lose.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discord_listener ./discord_listener

# Non-root, as with every other service. pwuser ships with the base image and
# owns the browser cache.
USER pwuser

CMD ["python", "-m", "discord_listener"]
