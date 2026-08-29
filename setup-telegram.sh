#!/bin/bash
# Wire up Telegram end to end: token in, chat id discovered, service restarted,
# test message delivered. Run on the server; it asks for the token once and
# handles the rest.
set -uo pipefail
cd "$(dirname "$0")"

API="${TELEGRAM_API:-https://api.telegram.org}"
ENV_FILE="monitor/.env"

# Replace the line if the key exists, append if it doesn't. Never duplicates,
# never disturbs anything else already configured.
set_env() {
  local key="$1" val="$2"
  touch "$ENV_FILE"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    local tmp; tmp=$(mktemp)
    grep -vE "^${key}=" "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'; }

# Parse rather than grep: whitespace in the JSON (which proxies and other
# encoders emit) makes a naive '"ok":true' match fail on a perfectly good reply.
api_ok()  { python3 -c "
import json,sys
try: print('yes' if json.load(sys.stdin).get('ok') else 'no')
except Exception: print('no')"; }
api_get() { python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
cur=d
for k in '$1'.split('.'):
    cur = cur.get(k) if isinstance(cur, dict) else None
    if cur is None: sys.exit()
print(cur)"; }

if [ ! -f "$ENV_FILE" ]; then
  cp monitor/.env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE from the example."
fi

# ---------------------------------------------------------------- 1. token
TOKEN=$(get_env TELEGRAM_BOT_TOKEN)
if [ -n "$TOKEN" ]; then
  echo "A bot token is already configured."
  read -rp "Replace it? [y/N] " ans
  [[ "$ans" =~ ^[Yy] ]] && TOKEN=""
fi

if [ -z "$TOKEN" ]; then
  echo
  echo "Paste the token BotFather gave you (it will not be echoed)."
  read -rsp "Token: " TOKEN
  echo
fi

if ! [[ "$TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "That does not look like a bot token (expected 1234567890:AA...)." >&2
  exit 1
fi

echo -n "Checking the token… "
ME=$(curl -s --max-time 20 "$API/bot$TOKEN/getMe")
if [ "$(printf '%s' "$ME" | api_ok)" != "yes" ]; then
  echo "rejected."
  echo "Telegram said: $(printf '%s' "$ME" | head -c 200)" >&2
  exit 1
fi
BOT=$(printf '%s' "$ME" | api_get result.username)
BOT="${BOT:-your bot}"
echo "ok — @$BOT"
set_env TELEGRAM_BOT_TOKEN "$TOKEN"

# ------------------------------------------------------------- 2. chat id
CHAT=$(get_env TELEGRAM_CHAT_ID)
if [ -z "$CHAT" ]; then
  echo
  echo "Now open Telegram and send @$BOT any message — 'hi' will do."
  echo "Waiting for it (Ctrl-C to give up)…"

  for _ in $(seq 1 60); do
    CHAT=$(curl -s --max-time 20 "$API/bot$TOKEN/getUpdates" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit()
ids = [u['message']['chat']['id'] for u in d.get('result', []) if 'message' in u]
print(ids[-1] if ids else '')
" 2>/dev/null)
    [ -n "$CHAT" ] && break
    printf '.'
    sleep 3
  done
  echo

  if [ -z "$CHAT" ]; then
    echo "No message arrived. Send one to @$BOT and run this again." >&2
    exit 1
  fi
  echo "Found you: chat id $CHAT"
fi
set_env TELEGRAM_CHAT_ID "$CHAT"

# ------------------------------------------------------------ 3. restart
if [ -d /run/systemd/system ] && systemctl list-unit-files monitor-api.service &>/dev/null; then
  echo -n "Restarting monitor-api… "
  sudo systemctl restart monitor-api && echo "ok" || echo "failed — check journalctl -u monitor-api"
  sleep 2
fi

# --------------------------------------------------------------- 4. prove
echo -n "Sending a test message… "
SEND=$(curl -s --max-time 20 -X POST "$API/bot$TOKEN/sendMessage" \
  -H 'Content-Type: application/json' \
  -d "{\"chat_id\":\"$CHAT\",\"parse_mode\":\"HTML\",\"text\":\"<b>Restock monitor</b>\nTelegram is wired up. Alerts will arrive here.\"}")
if [ "$(printf '%s' "$SEND" | api_ok)" = "yes" ]; then
  echo "delivered."
  echo
  echo "Done. Check your phone — you should have a message from @$BOT."
  echo "Nothing else to configure for alerts."
else
  echo "failed."
  echo "Telegram said: $(echo "$SEND" | head -c 200)" >&2
  exit 1
fi
