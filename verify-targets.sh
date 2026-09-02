#!/bin/bash
# Probe every strategy's real endpoints from THIS machine.
#
# Run this on the VPS before deploying. Every test in the suite mocks HTTP, so
# until this runs from the server's own IP, which platforms are actually
# reachable is unknown. Datacenter IP ranges are routinely refused by retailer
# bot protection, and that refusal is invisible from a laptop.
#
# Exit code is driven by Shopify alone: it is the strategy the project depends
# on. Target and Best Buy failing is a known-possible outcome, not a build break.

set -uo pipefail

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
STORE="${STORE:-https://satisfyrunning.com}"
TARGET_KEY="${TARGET_API_KEY:-9f36aeafbe60771e321a7cc95a78140772ab3e96}"

bold=$(tput bold 2>/dev/null || echo ""); dim=$(tput dim 2>/dev/null || echo "")
red=$(tput setaf 1 2>/dev/null || echo ""); grn=$(tput setaf 2 2>/dev/null || echo "")
ylw=$(tput setaf 3 2>/dev/null || echo ""); rst=$(tput sgr0 2>/dev/null || echo "")

RESULTS=()
SHOPIFY_OK=0

# Owned by the parent shell: probe() runs inside $( ), so anything it assigns is
# lost, but curl's -o writes straight to this path and does survive.
BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT

probe() {  # probe <url> -> echoes HTTP status; body lands in $BODY_FILE
  local url="$1" code
  code=$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 20 \
              -H "User-Agent: $UA" \
              -H 'Accept: application/json, text/plain, */*' \
              -H 'Accept-Language: en-US,en;q=0.9' \
              "$url" 2>/dev/null)
  echo "${code:-000}"
}

verdict() {  # verdict <code> <ok-pattern>
  case "$1" in
    200) echo "usable" ;;
    403|401) echo "BLOCKED" ;;
    429)     echo "rate-limited" ;;
    000)     echo "no route" ;;
    *)       echo "http $1" ;;
  esac
}

row() { RESULTS+=("$(printf '%-10s %-46s %-6s %s' "$1" "$2" "$3" "$4")"); }

MY_IP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null)
if ! printf '%s' "$MY_IP" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
  MY_IP="unknown (could not reach api.ipify.org)"
fi
echo "${bold}Probing from $MY_IP${rst}"
echo "${dim}Store under test: $STORE${rst}"
echo

# ---------------------------------------------------------------- Shopify
echo "${bold}Shopify${rst}"

CODE=$(probe "$STORE/products.json?limit=1")
V=$(verdict "$CODE")
HANDLE=""
if [ "$CODE" = "200" ] && grep -q '"products"' "$BODY_FILE" 2>/dev/null; then
  HANDLE=$(grep -oE '"handle"[[:space:]]*:[[:space:]]*"[^"]+"' "$BODY_FILE" \
             | head -1 | sed -E 's/.*"([^"]+)"$/\1/')
  SHOPIFY_OK=1
  if [ -n "$HANDLE" ]; then
    echo "  ${grn}✓${rst} products.json          200  (sample handle: $HANDLE)"
  else
    echo "  ${ylw}!${rst} products.json          200  responded, but no handle found in body"
  fi
else
  echo "  ${red}✗${rst} products.json          $CODE  $V"
fi
row "shopify" "/products.json" "$CODE" "$V"

# The per-product endpoint is what product watches actually poll.
if [ -n "$HANDLE" ]; then
  CODE=$(probe "$STORE/products/$HANDLE.js")
  V=$(verdict "$CODE")
  if [ "$CODE" = "200" ] && grep -q '"variants"' "$BODY_FILE" 2>/dev/null; then
    AVAIL=$(grep -oE '"available"[[:space:]]*:[[:space:]]*true' "$BODY_FILE" \
              | wc -l | tr -d ' ')
    echo "  ${grn}✓${rst} products/{handle}.js   200  ($AVAIL variant(s) in stock)"
  else
    echo "  ${red}✗${rst} products/{handle}.js   $CODE  $V"
    SHOPIFY_OK=0
  fi
  row "shopify" "/products/{handle}.js" "$CODE" "$V"
fi

# Fallback path, used when a store gates products.json.
CODE=$(probe "$STORE/collections/all.atom")
V=$(verdict "$CODE")
if [ "$CODE" = "200" ]; then
  echo "  ${grn}✓${rst} collections/all.atom   200  (fallback available)"
  SHOPIFY_OK=1   # atom alone is enough to run collection watches
else
  echo "  ${ylw}—${rst} collections/all.atom   $CODE  $V"
fi
row "shopify" "/collections/all.atom" "$CODE" "$V"

# ---------------------------------------------------------------- Target
echo
echo "${bold}Target (RedSky)${rst}"
CODE=$(probe "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1?key=$TARGET_KEY&tcin=00000000&channel=WEB")
V=$(verdict "$CODE")
case "$CODE" in
  200|400|404)
    # A 400/404 for a bogus TCIN still proves the endpoint answered us.
    echo "  ${grn}✓${rst} pdp_fulfillment_v1     $CODE  endpoint reachable from this IP" ;;
  403|401)
    echo "  ${red}✗${rst} pdp_fulfillment_v1     $CODE  ${red}BLOCKED${rst} — datacenter IP refused"
    echo "     ${dim}Expected on a VPS. Target watches will show as 'failing'.${rst}"
    echo "     ${dim}Fix: proxy this strategy through a residential IP or a Cloudflare Worker.${rst}" ;;
  *)
    echo "  ${ylw}?${rst} pdp_fulfillment_v1     $CODE  $V" ;;
esac
row "target" "redsky pdp_fulfillment_v1" "$CODE" "$V"

# A 410 means the endpoint was RETIRED, not that this IP is blocked, so no
# amount of proxying fixes it — the strategy needs pointing at whatever
# replaced it. These are the candidates; this prints which one answers so the
# rewrite is done against a measured shape rather than a guess.
if [ "$CODE" = "410" ] || [ "$CODE" = "404" ]; then
  echo
  echo "  ${ylw}pdp_fulfillment_v1 is gone. Probing replacements${rst}"
  # A real, long-lived TCIN so a 404 means "no such endpoint", not "no such item".
  PROBE_TCIN="${PROBE_TCIN:-87991564}"
  FOUND=""
  for EP in pdp_client_v1 product_fulfillment_v1 pdp_fulfillment_experience_v1 \
            product_summary_with_fulfillment_v1 pdp_variation_hierarchy_v1; do
    C=$(probe "https://redsky.target.com/redsky_aggregations/v1/web/$EP?key=$TARGET_KEY&tcin=$PROBE_TCIN&channel=WEB&page=%2Fp%2FA-$PROBE_TCIN")
    if [ "$C" = "200" ]; then
      echo "    ${grn}✓${rst} $(printf '%-38s' "$EP") $C"
      [ -z "$FOUND" ] && FOUND="$EP"
      # The shape matters as much as the status: print the key paths so the
      # rewrite knows where availability actually lives.
      python3 - "$BODY_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
def walk(o, p="", d=0):
    if d > 4: return
    if isinstance(o, dict):
        for k, v in list(o.items())[:14]:
            if isinstance(v, (dict, list)): walk(v, f"{p}.{k}", d + 1)
            elif any(t in k.lower() for t in
                     ("avail", "stock", "status", "quantity", "price", "title", "tcin")):
                print(f"       {p}.{k} = {str(v)[:52]}")
    elif isinstance(o, list) and o:
        walk(o[0], f"{p}[0]", d + 1)
try:
    walk(json.load(open(sys.argv[1])))
except Exception:
    pass
PYEOF
    else
      echo "    ${dim}·${rst} $(printf '%-38s' "$EP") $C"
    fi
  done
  echo
  if [ -n "$FOUND" ]; then
    echo "  ${grn}Paste this section back${rst} — target.py gets rewritten against $FOUND."
  else
    echo "  ${ylw}Every candidate is dead too.${rst} The honest fix is to drop the"
    echo "  ${dim}target strategy rather than ship one that always fails.${rst}"
  fi
fi

# ---------------------------------------------------------------- Best Buy
echo
echo "${bold}Best Buy${rst}"
if [ -z "${BESTBUY_API_KEY:-}" ]; then
  echo "  ${ylw}—${rst} skipped: BESTBUY_API_KEY not set"
  echo "     ${dim}Free key: https://developer.bestbuy.com — export it and re-run.${rst}"
  row "bestbuy" "(skipped, no API key)" "—" "skipped"
else
  CODE=$(probe "https://api.bestbuy.com/v1/products(sku=6521430)?apiKey=$BESTBUY_API_KEY&format=json&show=sku,name")
  V=$(verdict "$CODE")
  if [ "$CODE" = "200" ]; then
    echo "  ${grn}✓${rst} products API           200  key valid, endpoint reachable"
  elif [ "$CODE" = "403" ]; then
    echo "  ${red}✗${rst} products API           403  key rejected or over quota"
  else
    echo "  ${ylw}?${rst} products API           $CODE  $V"
  fi
  row "bestbuy" "api.bestbuy.com/v1/products" "$CODE" "$V"
fi

# ---------------------------------------------------------------- summary
echo
echo "${bold}Summary${rst}"
printf '%-10s %-46s %-6s %s\n' "STRATEGY" "ENDPOINT" "CODE" "VERDICT"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo

if [ "$SHOPIFY_OK" -eq 1 ]; then
  echo "${grn}${bold}Shopify is usable from this machine.${rst} The core of the app will work."
  echo "Any BLOCKED rows above are optional strategies — those watches will report"
  echo "as failing rather than returning wrong answers, which is by design."
  exit 0
else
  echo "${red}${bold}Shopify is NOT reachable from this machine.${rst}"
  echo "This is the strategy everything depends on. Before deploying, either:"
  echo "  • run the fetcher from a residential IP (a Pi over Tailscale), or"
  echo "  • route requests through a Cloudflare Worker as a second egress identity."
  exit 1
fi
