#!/bin/bash
# Pull the tracked branch and restart the service — but only if the new code
# passes its own tests, and only if the restart comes up healthy.
#
# This is NOT deploy-monitor.sh. That script is the installer: it apt-installs
# Caddy and finishes by probing live Shopify, which is right once and far too
# heavy every five minutes. This is the narrow update path.
#
# The whole design rests on one point: an auto-deploy that can ship broken code
# to a monitor is worse than no auto-deploy at all. The reason this app exists
# is to catch a drop you would otherwise miss, so quietly replacing a working
# monitor with a broken one defeats the purpose. Hence: test before restart,
# roll back if the restart is unhealthy, and never touch a running service on
# the way to failing.
# No `set -e`: this script handles its own failures so it can roll back, which
# means a failed cd would otherwise run the whole update in the wrong directory.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

APP_DIR="$(pwd)"
BRANCH="$(cat .autodeploy-branch 2>/dev/null || echo main)"
VENV="$APP_DIR/monitor/venv"
PY="$VENV/bin/python"
HEALTH="${HEALTH_URL:-http://127.0.0.1:8003/health}"
SYSTEMCTL="${SYSTEMCTL:-sudo systemctl}"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Telegram reuses the token already in monitor/.env via the app's own notifier,
# so there is no second copy of the credential and nothing new to configure.
notify() {
  [ -x "$PY" ] || return 0
  "$PY" - "$1" <<'PYEOF' >/dev/null 2>&1 || true
import asyncio, sys
from monitor.notify import telegram
if telegram.configured():
    asyncio.run(telegram.send_text(sys.argv[1]))
PYEOF
}

health_ok() {
  for _ in $(seq 1 10); do
    curl -sf --max-time 3 "$HEALTH" >/dev/null && return 0
    sleep 2
  done
  return 1
}

# Put the tree back and bring the old code up. Used when new code fails its
# tests (service never restarted) and when a restart comes up unhealthy.
#
# Reverting the code is not enough on its own: if this update installed new
# dependencies, those are still in the venv and may be the very thing that
# broke the service — a rollback that leaves them behind would not actually
# roll anything back.
rollback() {
  log "rolling back to ${1:0:7}"
  git reset --hard --quiet "$1"
  if [ "${2:-}" = "deps" ]; then
    log "restoring dependencies"
    "$VENV/bin/pip" install -q -r monitor/requirements.txt || true
  fi
  $SYSTEMCTL restart monitor-api || true
}

git fetch --quiet origin "$BRANCH" || { log "fetch failed"; exit 1; }

PREV="$(git rev-parse HEAD)"
NEXT="$(git rev-parse "origin/$BRANCH")"

# A quiet tick must stay quiet, or the timer turns into noise you learn to
# ignore — the same failure mode as an over-broad keyword watch.
if [ "$PREV" = "$NEXT" ]; then
  exit 0
fi

log "updating ${PREV:0:7} -> ${NEXT:0:7} on $BRANCH"
git reset --hard --quiet "$NEXT" || { log "reset failed"; exit 1; }

CHANGED="$(git diff --name-only "$PREV" "$NEXT")"

DEPS=""
if grep -q '^monitor/requirements.txt$' <<<"$CHANGED"; then
  DEPS="deps"
  log "dependencies changed — installing"
  if ! "$VENV/bin/pip" install -q -r monitor/requirements.txt; then
    log "pip install failed"
    rollback "$PREV" "$DEPS"
    notify "⚠️ <b>Auto-deploy failed</b>
<code>pip install</code> failed for <code>${NEXT:0:7}</code>.
Still running <code>${PREV:0:7}</code>."
    exit 1
  fi
fi

# The gate. A failure here must leave the running service completely untouched,
# so this runs before anything is restarted.
log "running tests"
if ! TEST_OUT="$("$PY" -m pytest monitor/tests -q 2>&1)"; then
  log "TESTS FAILED — not restarting"
  echo "$TEST_OUT" | tail -20
  SUMMARY="$(echo "$TEST_OUT" | grep -E '^[0-9]+ (passed|failed)' | tail -1)"
  # The service was never restarted, so put the tree (and any new deps) back
  # without touching it.
  git reset --hard --quiet "$PREV"
  if [ -n "$DEPS" ]; then
    log "restoring dependencies"
    "$VENV/bin/pip" install -q -r monitor/requirements.txt || true
  fi
  notify "⚠️ <b>Auto-deploy blocked</b>
Commit <code>${NEXT:0:7}</code> failed its tests, so it was not deployed.
<code>${SUMMARY:-see journalctl -u restock-autodeploy}</code>
Still running <code>${PREV:0:7}</code>."
  exit 1
fi

if grep -q '^monitor-api.service$' <<<"$CHANGED"; then
  log "systemd unit changed — reinstalling"
  sudo cp monitor-api.service /etc/systemd/system/monitor-api.service
  sudo systemctl daemon-reload
fi

if grep -q '^Caddyfile$' <<<"$CHANGED"; then
  log "Caddyfile changed — reloading"
  sudo cp Caddyfile /etc/caddy/Caddyfile
  sudo systemctl reload caddy || sudo systemctl restart caddy
fi

log "restarting monitor-api"
$SYSTEMCTL restart monitor-api

if ! health_ok; then
  log "HEALTH CHECK FAILED after restart"
  rollback "$PREV" "$DEPS"
  BACK="not healthy either — check journalctl -u monitor-api"
  health_ok && BACK="the previous version is back up"
  notify "🔴 <b>Auto-deploy rolled back</b>
<code>${NEXT:0:7}</code> passed tests but did not come up healthy.
Reverted to <code>${PREV:0:7}</code> — $BACK."
  exit 1
fi

SUBJECT="$(git log -1 --pretty=%s "$NEXT" | cut -c1-90)"
log "deployed ${NEXT:0:7}"
notify "✅ <b>Deployed</b> <code>${NEXT:0:7}</code>
$SUBJECT"
