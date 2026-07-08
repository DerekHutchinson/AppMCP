#!/usr/bin/env bash
set -euo pipefail

APP_NAME="appmcp"
IMAGE_NAME="appmcp:latest"
HOST_PORT="8017"
CONTAINER_PORT="8000"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "ERROR: .env file missing at $APP_DIR/.env"
  exit 1
fi

echo "Deploying $APP_NAME from $APP_DIR"

# --- app history: clone the history repo into ./data/app-history (mounted at
# /app/data/app-history) so the container can commit + push. Best-effort: a
# history problem must never abort the deploy. tr -d '\r' guards CRLF .env files.
env_val() { grep -E "^$1=" .env | head -n1 | cut -d= -f2- | tr -d '\r' || true; }
mkdir -p "$APP_DIR/data"
HISTORY_DIR="$APP_DIR/data/app-history"
if [ "$(env_val GIT_HISTORY_ENABLED)" = "true" ] && [ ! -d "$HISTORY_DIR/.git" ]; then
  HIST_URL="$(env_val GIT_HISTORY_REPO_URL)"
  HIST_TOKEN="$(env_val GIT_HISTORY_TOKEN)"
  if [ -z "$HIST_URL" ] || [ -z "$HIST_TOKEN" ] || [ "$HIST_TOKEN" = "your-github-pat" ]; then
    echo "WARNING: GIT_HISTORY_ENABLED=true but GIT_HISTORY_REPO_URL/GIT_HISTORY_TOKEN not set; skipping history clone."
  else
    AUTHED_URL="$(printf '%s' "$HIST_URL" | sed -E "s#^https://#https://x-access-token:${HIST_TOKEN}@#")"
    echo "Cloning app history repo into $HISTORY_DIR ..."
    git clone "$AUTHED_URL" "$HISTORY_DIR" || echo "WARNING: history clone failed; app history will be skipped."
  fi
fi

docker build -t "$IMAGE_NAME" .

docker rm -f "$APP_NAME" >/dev/null 2>&1 || true

docker run -d \
  --name "$APP_NAME" \
  --restart unless-stopped \
  --env-file "$APP_DIR/.env" \
  -p 127.0.0.1:${HOST_PORT}:${CONTAINER_PORT} \
  -v "$APP_DIR/data:/app/data" \
  "$IMAGE_NAME"

sleep 3
curl -fsS "http://127.0.0.1:${HOST_PORT}/healthz"

echo ""
echo "Deploy complete."
docker ps --filter "name=$APP_NAME"