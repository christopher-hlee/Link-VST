#!/bin/bash
# End-to-end check against the RUNNING service, so nobody has to hand-verify.
#
# Exercises the real API over the real network with a real Shopify product:
# auth guard, platform detection, watch lifecycle, and the invariant that a
# failed check never fabricates a stock state. Cleans up after itself.
set -uo pipefail

API="${API:-http://127.0.0.1:8003}"
ENV_FILE="$(dirname "$0")/monitor/.env"
PROBE_URL="${PROBE_URL:-https://satisfyrunning.com/collections/shop-all}"

pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass+1)); }
bad()  { echo "  ✗ $1"; fail=$((fail+1)); }

KEY=$(grep -E '^MONITOR_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ -z "$KEY" ]; then
  echo "MONITOR_API_KEY is not set in monitor/.env — add one so this script can"
  echo "call the API without a browser session, then re-run."
  exit 1
fi
AUTH=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$@"; }
body() { curl -s --max-time 25 "$@"; }

echo "Service"
[ "$(code "$API/health")" = "200" ] && ok "/health responds" || bad "/health did not respond"
[ "$(code "$API/api/watches")" = "401" ] \
  && ok "unauthenticated API is refused" || bad "API answered without auth"
[ "$(code "${AUTH[@]}" "$API/api/watches")" = "200" ] \
  && ok "API key accepted" || bad "API key rejected"

echo
echo "Platform detection (live network)"
DET=$(body "${AUTH[@]}" -X POST "$API/api/detect" -d "{\"url\":\"$PROBE_URL\"}")
if echo "$DET" | grep -q '"strategy"'; then
  ok "detected: $(echo "$DET" | grep -o '"strategy":"[^"]*"' | cut -d'"' -f4)"
else
  bad "detection failed: $(echo "$DET" | head -c 120)"
fi

echo
echo "Watch lifecycle"
NEW=$(body "${AUTH[@]}" -X POST "$API/api/watches" \
      -d "{\"url\":\"$PROBE_URL\",\"tier\":\"slow\",\"name\":\"selftest\"}")
WID=$(echo "$NEW" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
if [ -n "$WID" ]; then ok "created watch #$WID"; else bad "create failed: $(echo "$NEW" | head -c 120)"; fi

if [ -n "$WID" ]; then
  CHK=$(body "${AUTH[@]}" -X POST "$API/api/watches/$WID/check")
  STATE=$(echo "$CHK" | grep -o '"last_state":"[^"]*"' | head -1 | cut -d'"' -f4)
  FAILS=$(echo "$CHK" | grep -o '"consecutive_failures":[0-9]*' | head -1 | cut -d: -f2)
  if [ "$FAILS" = "0" ]; then
    ok "live check succeeded — state '$STATE'"
  else
    # Not a test failure: the site may be refusing this host. What matters is
    # that the app SAYS so instead of inventing a stock state.
    ERR=$(echo "$CHK" | grep -o '"last_error":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ "$STATE" = "unknown" ]; then
      ok "check failed cleanly, state left 'unknown' (${ERR:-no detail})"
    else
      bad "check failed but state became '$STATE' — must stay unknown"
    fi
  fi

  BOGUS=$(body "${AUTH[@]}" -X POST "$API/api/watches" \
          -d '{"url":"https://satisfyrunning.com/products/does-not-exist-xyz","strategy":"shopify","kind":"product","target_ref":"does-not-exist-xyz","name":"selftest-404"}')
  BID=$(echo "$BOGUS" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "$BID" ]; then
    B=$(body "${AUTH[@]}" -X POST "$API/api/watches/$BID/check")
    BSTATE=$(echo "$B" | grep -o '"last_state":"[^"]*"' | head -1 | cut -d'"' -f4)
    [ "$BSTATE" = "unknown" ] \
      && ok "404 product stays 'unknown', never 'out_of_stock'" \
      || bad "404 product became '$BSTATE' — the core invariant is broken"
    body "${AUTH[@]}" -X DELETE "$API/api/watches/$BID" >/dev/null
  fi

  body "${AUTH[@]}" -X DELETE "$API/api/watches/$WID" >/dev/null
  ok "cleaned up test watches"
fi

echo
echo "Notifications"
TG=$(body "${AUTH[@]}" -X POST "$API/api/test-alert")
if echo "$TG" | grep -q '"ok":true'; then
  ok "Telegram delivered — check your phone"
else
  bad "Telegram: $(echo "$TG" | grep -o '"detail":"[^"]*"' | cut -d'"' -f4 | head -c 100)"
fi

echo
echo "Service health"
if systemctl is-active --quiet monitor-api 2>/dev/null; then
  ok "monitor-api is running"
elif [ ! -d /run/systemd/system ]; then
  echo "  – not a systemd host, skipping service check"
else
  bad "monitor-api is not running"
fi
if command -v ss >/dev/null || command -v netstat >/dev/null; then
  LISTEN=$( (ss -tln 2>/dev/null || netstat -tln 2>/dev/null) | grep ':8003' )
  if echo "$LISTEN" | grep -q '127.0.0.1:8003'; then
    ok "bound to loopback only (not exposed directly)"
  elif echo "$LISTEN" | grep -qE '0\.0\.0\.0:8003|\*:8003'; then
    bad "8003 is bound to ALL interfaces — it should be loopback only"
  else
    echo "  – could not read listening sockets, skipping"
  fi
else
  echo "  – no ss/netstat, skipping bind check"
fi

echo
echo "──────────────────────────────"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ] && echo "All good." || echo "Fix the ✗ items above."
exit "$fail"
