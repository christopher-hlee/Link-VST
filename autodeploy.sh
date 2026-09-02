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
# The snapshot below runs from /tmp, where $0 says nothing about where the
# checkout is, so the directory has to be carried across explicitly.
if [ -n "${AUTODEPLOY_DIR:-}" ]; then
  cd "$AUTODEPLOY_DIR" || exit 1
else
  cd "$(dirname "$0")" || exit 1
fi

# This script updates the checkout it lives in, so `git reset` rewrites the very
# file bash is executing. Bash reads a script lazily by byte offset, so a swap
# mid-run can resume execution at the wrong place. Run from a snapshot instead.
if [ "${AUTODEPLOY_SNAPSHOT:-}" != "1" ]; then
  SNAP="$(mktemp /tmp/autodeploy.XXXXXX.sh)" || exit 1
  cp "$0" "$SNAP" || exit 1
  export AUTODEPLOY_SNAPSHOT=1 AUTODEPLOY_DIR="$PWD"
  bash "$SNAP" "$@"; rc=$?
  rm -f "$SNAP"
  exit $rc
fi

APP_DIR="$(pwd)"
BRANCH="$(cat .autodeploy-branch 2>/dev/null || echo main)"
VENV="$APP_DIR/monitor/venv"
PY="$VENV/bin/python"
HEALTH="${HEALTH_URL:-http://127.0.0.1:8003/health}"
SYSTEMCTL="${SYSTEMCTL:-sudo systemctl}"
# Remembers the commit that last failed, so the same bad commit is reported
# once instead of every five minutes forever.
FAILED_MARK="$APP_DIR/.autodeploy-failed"

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

# Already tried this exact commit and it failed. Retrying every five minutes
# changes nothing and re-sends the same alert, which is how a useful signal
# turns into one you swipe away. Push a fix (or delete the marker) to retry.
if [ "$(cat "$FAILED_MARK" 2>/dev/null)" = "$NEXT" ]; then
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

# Undo this attempt without touching the service, which has not been restarted
# at this point. Records the commit so the next tick stays silent about it.
abandon() {
  git reset --hard --quiet "$PREV"
  if [ -n "$DEPS" ]; then
    log "restoring dependencies"
    "$VENV/bin/pip" install -q -r monitor/requirements.txt || true
  fi
  echo "$NEXT" > "$FAILED_MARK"
}

# The gate needs a test runner, and requirements.txt does not carry one — the
# venv is built for running the service, not for testing it. Without this the
# gate reports "tests failed" when it means "cannot run tests", which sends you
# looking at the wrong thing entirely.
if ! "$PY" -c "import pytest" >/dev/null 2>&1 \
   || grep -q '^monitor/requirements-dev.txt$' <<<"$CHANGED"; then
  log "installing the test runner"
  "$VENV/bin/pip" install -q -r monitor/requirements-dev.txt >/dev/null 2>&1
fi

if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  log "CANNOT RUN TESTS — pytest is unavailable"
  abandon
  notify "⚠️ <b>Auto-deploy cannot verify</b>
The test runner is missing and could not be installed, so <code>${NEXT:0:7}</code>
was not deployed — this is the machine, not the code.
<code>monitor/venv/bin/pip install -r monitor/requirements-dev.txt</code>
Still running <code>${PREV:0:7}</code>."
  exit 1
fi

# The gate. A failure here must leave the running service completely untouched,
# so this runs before anything is restarted.
log "running tests"
if ! TEST_OUT="$("$PY" -m pytest monitor/tests -q 2>&1)"; then
  log "TESTS FAILED — not restarting"
  echo "$TEST_OUT" | tail -20
  # Quote what actually happened. "See journalctl" is a way of saying the alert
  # does not know, and sends you to a terminal to find out what it could have
  # told you.
  SUMMARY="$(echo "$TEST_OUT" | grep -E '^(FAILED|ERROR|[0-9]+ (passed|failed))' | tail -3)"
  abandon
  notify "⚠️ <b>Auto-deploy blocked</b>
Commit <code>${NEXT:0:7}</code> failed its tests, so it was not deployed.
<code>${SUMMARY:-$(echo "$TEST_OUT" | tail -3)}</code>
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
  echo "$NEXT" > "$FAILED_MARK"
  BACK="not healthy either — check journalctl -u monitor-api"
  health_ok && BACK="the previous version is back up"
  notify "🔴 <b>Auto-deploy rolled back</b>
<code>${NEXT:0:7}</code> passed tests but did not come up healthy.
Reverted to <code>${PREV:0:7}</code> — $BACK."
  exit 1
fi

rm -f "$FAILED_MARK"
SUBJECT="$(git log -1 --pretty=%s "$NEXT" | cut -c1-90)"
log "deployed ${NEXT:0:7}"
notify "✅ <b>Deployed</b> <code>${NEXT:0:7}</code>
$SUBJECT"
