#!/bin/bash
# Install/update the restock monitor. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
VENV="$APP_DIR/monitor/venv"

echo "==> Installing the restock monitor from $APP_DIR"

if ! command -v caddy &>/dev/null; then
  echo "--> Installing Caddy"
  sudo apt-get update -qq
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy
fi

if [ ! -d "$VENV" ]; then
  echo "--> Creating virtualenv"
  python3 -m venv "$VENV"
fi

echo "--> Installing Python dependencies"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r monitor/requirements.txt

if [ ! -f monitor/.env ]; then
  cp monitor/.env.example monitor/.env
  chmod 600 monitor/.env
  echo
  echo "!! monitor/.env was just created and is INCOMPLETE."
  echo "!! 1. Run:  $VENV/bin/python -m monitor.hashpw"
  echo "!!    and paste both printed lines into monitor/.env"
  echo "!! 2. Add TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and HEALTHCHECK_URL"
  echo "!! Then re-run this script."
  exit 1
fi

if ! grep -q '^MONITOR_PASSWORD_HASH=scrypt' monitor/.env; then
  echo "!! MONITOR_PASSWORD_HASH is not set in monitor/.env."
  echo "!! Run: $VENV/bin/python -m monitor.hashpw"
  exit 1
fi

chmod 600 monitor/.env
mkdir -p monitor/data

echo "--> Installing systemd unit"
sudo cp monitor-api.service /etc/systemd/system/monitor-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now monitor-api
sudo systemctl restart monitor-api

echo "--> Installing Caddy site"
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy || sudo systemctl restart caddy

sleep 2
echo
if curl -sf http://127.0.0.1:8003/health >/dev/null; then
  echo "==> Monitor is up. https://0iu6eih.cserverhost.cloud"
else
  echo "!! Health check failed. Logs: journalctl -u monitor-api -n 50 --no-pager"
  exit 1
fi

# The test suite mocks every HTTP call, so this is the first moment we learn
# which platforms this server's IP can actually reach.
echo
echo "==> Probing live endpoints from this host"
if ! ./verify-targets.sh; then
  echo
  echo "!! Shopify is unreachable from this IP. The service is running, but the"
  echo "!! strategy the app depends on will fail. See the guidance above."
  exit 1
fi
