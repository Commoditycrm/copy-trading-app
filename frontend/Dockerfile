FROM node:20-alpine

WORKDIR /app

# Install deps first so changes to source code don't blow away the npm cache layer.
COPY package.json package-lock.json ./
RUN npm install --legacy-peer-deps --no-audit --no-fund

# App source + Next.js build.
COPY . .
RUN npm run build

EXPOSE 3000

# Next.js production server. `next start` reads the build output from .next/.
# Bind to 0.0.0.0 so docker compose's internal network can reach it from the
# caddy reverse proxy container (BACKEND_URL=http://backend:8000 in override).
CMD ["sh", "-c", "npm start -- --hostname 0.0.0.0 --port 3000"]
