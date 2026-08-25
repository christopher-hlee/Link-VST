# Restock Monitor

Watches product pages and pings Telegram the moment something comes back in
stock or a new drop lands. Built for things that sell out before you can react —
Satisfy drops, the Cafelat Robot at US retailers, limited console editions.

Runs alongside the LinkVST API on the same box (port 8003 vs 8002) and follows
the same conventions: raw `sqlite3` with WAL, FastAPI `lifespan` → `init_db()`,
routers under `/api`, single-file dashboard with no build step.

## The rule that shapes everything

**A failed check never becomes a stock state.** A 403, a timeout, or a parse
error leaves the last known state untouched and increments a failure counter.
Treating "we couldn't reach the site" as "it's sold out" is how monitors die
quietly while still looking healthy — the dashboard would show a calm grid of
sold-out items and you'd never know. After five consecutive failures the watch
alerts you that it's broken.

## Polling

Three tiers, chosen per watch. Not a flat 60 seconds — that's five times the
ban risk to save about two minutes on a restock that usually sits for hours.

| Tier | Interval | For |
|------|----------|-----|
| slow | 15 min   | announcements, feeds |
| base | 5 min    | default — restocks |
| fast | 45 s     | only while armed for a known drop window |

Three details do the real work:

- **Jitter** (±20%) — a perfectly periodic request pattern is a signature.
- **ETag / If-None-Match** — most polls come back `304`, cheap and low-profile.
- **Arming** — `POST /api/watches/{id}/arm` switches to the fast tier for a
  set window, so you only spend aggression when a drop is actually expected.

## Strategies

Auto-detected from the URL you paste — you never pick one.

- **`shopify`** — `/products/{handle}.js` for availability, `/products.json` for
  new-handle detection, Atom feed as fallback when JSON is gated. Alerts carry a
  `/cart/{variant_id}:1` permalink that drops the item straight into your cart.
- **`target`** — RedSky, including per-store pickup quantities. Local pickup is
  far less contested than online stock.
- **`bestbuy`** — the official developer API. Needs a free `BESTBUY_API_KEY`.

Both retailer APIs are commonly refused from datacenter IP ranges. That surfaces
as a `failing` watch with the HTTP status, not as a silent wrong answer.

## Setup

```bash
./harden-server.sh          # once, as root, on a fresh VPS — key-only SSH, ufw, fail2ban
./deploy-monitor.sh         # venv, systemd unit, Caddy + TLS
```

`deploy-monitor.sh` stops and tells you what's missing until `monitor/.env` is
complete. Generate the dashboard credentials on the server:

```bash
monitor/venv/bin/python -m monitor.hashpw
```

Then add the Telegram values. Create a bot with `@BotFather`, message it once,
and read your chat id from:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Finally set `HEALTHCHECK_URL` to a [healthchecks.io](https://healthchecks.io)
ping URL. This is the dead-man's switch: it emails you when the pings stop,
through a different path from Telegram, so one outage can't silence both.

Verify delivery from the dashboard's **Test Telegram** button before trusting it.

## API

| Method | Path | |
|---|---|---|
| GET | `/health` | unauthenticated |
| POST | `/api/login` · `/api/logout` · GET `/api/me` | session cookie |
| POST | `/api/detect` | sniff a URL's platform |
| GET/POST | `/api/watches` | list / create |
| GET/PATCH/DELETE | `/api/watches/{id}` | |
| POST | `/api/watches/{id}/check` · `/arm` · `/disarm` | |
| GET | `/api/events` | alert feed |
| POST | `/api/test-alert` | Telegram self-test |

Browser access uses a signed session cookie; `MONITOR_API_KEY` gives Bearer
access for scripts.

## Tests

```bash
monitor/venv/bin/python -m pytest monitor/tests -c monitor/pytest.ini
```

52 tests, no network — HTTP is mocked with `respx`. The suite pins the failure
invariant, alert deduplication, the Atom fallback, and the full
sold-out → restock → alert cycle against a temp database.

## Not built yet

Playwright for JS-rendered sites; a Cloudflare Worker as a second egress
identity for retailers that block the VPS; the Zelda console watcher (no SKU
exists yet — it needs an announcement watcher first). Auto-checkout is
deliberately out of scope: this notifies, it doesn't buy.
