#!/bin/bash
# One-time setup: pin the branch, install the auto-deploy timer, deploy now.
# Safe to re-run — every step is idempotent.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

APP_DIR="$(pwd)"
BRANCH="${BRANCH:-claude/ionos-vps-product-monitor-gflhfy}"
REMOTE="${REMOTE:-https://github.com/christopher-hlee/Link-VST.git}"

echo "==> Auto-deploy setup for $APP_DIR"

if [ "$(id -un)" = "root" ]; then
  echo "!! Run this as 'platform', not root — a root git pull leaves files the"
  echo "!! service user cannot write."
  exit 1
fi

# Pin the remote and branch rather than assuming how this clone was created.
git remote set-url origin "$REMOTE" 2>/dev/null || git remote add origin "$REMOTE"
echo "$BRANCH" > .autodeploy-branch
git fetch --quiet origin "$BRANCH" || { echo "!! Cannot reach $REMOTE"; exit 1; }
echo "--> Tracking $BRANCH"

chmod +x autodeploy.sh deploy-monitor.sh verify-targets.sh selftest.sh 2>/dev/null

# The timer goes in FIRST and deliberately before the installer. deploy-monitor.sh
# ends with selftest.sh, which exits non-zero when MONITOR_API_KEY is unset — and
# aborting here would leave auto-deploy uninstalled because of an unrelated
# missing key. The timer is inert until a new commit appears, so installing it
# ahead of a possibly-incomplete deploy is safe.
echo "==> Installing the auto-deploy timer"
sed "s#^WorkingDirectory=.*#WorkingDirectory=$APP_DIR#; \
     s#^ExecStart=.*#ExecStart=/usr/bin/flock -n /tmp/restock-autodeploy.lock $APP_DIR/autodeploy.sh#" \
    restock-autodeploy.service | sudo tee /etc/systemd/system/restock-autodeploy.service >/dev/null
sudo cp restock-autodeploy.timer /etc/systemd/system/restock-autodeploy.timer
sudo systemctl daemon-reload
sudo systemctl enable --now restock-autodeploy.timer

echo "==> Running the full installer once (Caddy, venv, unit, live probes)"
if ./deploy-monitor.sh; then
  INSTALL_OK=yes
else
  INSTALL_OK=no
  echo
  echo "!! deploy-monitor.sh reported a problem — see the output above."
  echo "!! The auto-deploy timer is installed regardless, so once that is fixed"
  echo "!! the next push deploys normally."
fi

echo
echo "===================================================================="
if [ "$INSTALL_OK" = yes ]; then
  echo " Done. Every push to $BRANCH now deploys itself."
else
  echo " Timer installed, but the initial deploy needs attention (above)."
fi
echo "===================================================================="
systemctl list-timers restock-autodeploy --no-pager 2>/dev/null
echo
echo "  Watch it:    journalctl -u restock-autodeploy -f"
echo "  Deploy now:  sudo systemctl start restock-autodeploy"
echo "  Turn it off: sudo systemctl disable --now restock-autodeploy.timer"
