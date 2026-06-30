FROM node:20-alpine

WORKDIR /app

# Install deps first so changes to source code don't blow away the npm cache layer.
# `npm ci` (not `npm install`) installs EXACTLY what package-lock.json pins and
# verifies each package's integrity hash — reproducible, and it can't silently
# pull a different/newer version of a dependency at build time (supply-chain).
COPY package.json package-lock.json ./
RUN npm ci --legacy-peer-deps --no-audit --no-fund

# App source + Next.js build.
COPY . .
RUN npm run build

EXPOSE 3000

# Run as the built-in non-root `node` user (uid 1000). Build steps above ran
# as root and produced world-readable .next/node_modules, so the runtime user
# can read them; all writes go to the noexec tmpfs mounts (/tmp, .next/cache)
# declared in docker-compose.yml. Combined with read_only + cap_drop there, a
# poisoned dependency has nowhere writable+executable to drop and run a binary.
USER node

# Next.js production server. `next start` reads the build output from .next/.
# Bind to 0.0.0.0 so docker compose's internal network can reach it from the
# caddy reverse proxy container (BACKEND_URL=http://backend:8000 in override).
CMD ["sh", "-c", "npm start -- --hostname 0.0.0.0 --port 3000"]
