#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "==> Setting up LinkVST server..."

# Install system deps (fluidsynth + GM soundfont for audio preview)
if ! command -v fluidsynth &>/dev/null; then
  sudo apt-get install -y fluidsynth fluid-soundfont-gm
fi

if [ ! -d server/venv ]; then
  python3 -m venv server/venv
fi

source server/venv/bin/activate
pip install -q -r server/requirements.txt

if [ ! -f server/.env ]; then
  echo "WARNING: server/.env not found. Copy server/.env.example and fill in keys."
  cp server/.env.example server/.env
fi

sudo cp link-vst-api.service /etc/systemd/system/link-vst-api.service
sudo systemctl daemon-reload
sudo systemctl enable link-vst-api
sudo systemctl restart link-vst-api

echo "==> Done. API running on http://localhost:8002"
echo "    Health: curl http://localhost:8002/health"
