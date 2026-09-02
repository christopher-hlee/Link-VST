# Restock Monitor

Watches product pages and pings Telegram the moment something comes back in
stock or a new drop lands. Built for things that sell out before you can react —
Satisfy drops, the Cafelat Robot at US retailers, limited console editions.

FastAPI + SQLite + APScheduler, no build step and no frontend framework. Binds
`127.0.0.1:8003` behind Caddy, so it coexists with anything else already on the
box.

## The dashboard

A single sheet grouped by what needs you rather than by when it was added: what
to act on, then what is held, then what is broken, then everything waiting
collapsed into a table. It stays readable at forty watches because most of them
are one row in the last group.

In-stock apparel shows one cart chip per available size, the preferred size
promoted. Sold-out sizes are absent rather than disabled — a greyed button
invites a tap that cannot work. Single-variant products collapse to one
"Add to cart".

A drop that has fired gets its own card above the list, naming each new item
with its price and a cart link, because the watch itself goes straight back to
watching and would otherwise show nothing. Cards clear after 48 hours or when
dismissed. Below everything, **Recent alerts** lists every alert sent, so the
dashboard can be checked against Telegram instead of only agreeing with it.

## A drop is a list; a restock is one transition

They alert differently on purpose. A restock is one item changing state, so it
gets one cart link per returned size. A drop is *n* items appearing at once, so
it gets one link per item — each pointing at that product, never at the
collection being polled. The handle in `products.json` is the store's own
canonical identifier, so `{store}/products/{handle}` needs no search and no
extra request, and the variants in that same response supply the cart
permalinks. A drop of exactly one item is treated as a restock and gets sizes.

## The rule that shapes everything

**A failed check never becomes a stock state.** A 403, a timeout, or a parse
error leaves the last known state untouched and increments a failure counter.
Treating "we couldn't reach the site" as "it's sold out" is how monitors die
quietly while still looking healthy — the dashboard would show a calm grid of
sold-out items and you'd never know. After five consecutive failures the watch
alerts you that it's broken and pauses itself.

The same principle produces **held**: an item back in stock, but not in the size
you watch. That is not sold out either. A held row names what came back, what
you watch, and says nothing was sent on purpose — so silence always carries a
reason you can read.

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

- **`announce`** — new entries in an Atom/RSS feed or a Shopify search endpoint,
  filtered by keyword. This is the one that can track something with no product
  page yet; everything else presupposes a URL to poll. Keywords take two forms:
  `a, b` fires on either, `a + b` fires only when both appear. On a busy feed
  the second is almost always what you want — one broad term matches everything
  and the alert becomes noise you learn to ignore, which is worse than no alert.

Both retailer APIs are commonly refused from datacenter IP ranges. That surfaces
as a `failing` watch with the HTTP status, not as a silent wrong answer — and
`worker/` contains an optional Cloudflare Worker that re-issues requests for
specific hosts from Cloudflare's edge, giving the monitor a second network
identity. Deploy it only once `verify-targets.sh` shows you need it.

## Setup

```bash
./harden-server.sh          # once, as root, on a fresh VPS — key-only SSH, ufw, fail2ban
./verify-targets.sh         # which platforms can this IP actually reach?
./deploy-monitor.sh         # venv, systemd unit, Caddy + TLS
```

`verify-targets.sh` is worth running before anything else and is worth trusting
over the test suite. Every test here mocks HTTP, so until this runs from the
server itself, which strategies work from that IP is unknown — and a datacenter
IP being refused by retailer bot protection is invisible from a laptop. It exits
non-zero only when **Shopify** fails, since that's the strategy the project
depends on; Target and Best Buy being blocked is a known-possible outcome and
prints guidance instead of failing the run. `deploy-monitor.sh` runs it too,
after the health check.

`deploy-monitor.sh` stops and tells you what's missing until `monitor/.env` is
complete. Generate the dashboard credentials on the server:

```bash
monitor/venv/bin/python -m monitor.hashpw
```

Then wire up Telegram with one command — it validates the token, waits for you
to message the bot, discovers your chat id, restarts the service and sends a
test message:

```bash
./setup-telegram.sh
```

Create the bot with `@BotFather` first; that is the only part that happens off
the server.

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

## Alerting tiers

Watches are `info` or `critical`. Everything goes to Telegram; critical also
goes to ntfy at urgent priority, which unlike Telegram pierces Do Not Disturb.
A watch that breaks escalates to critical regardless of its own setting, because
a monitor that has stopped seeing the site is urgent however it was configured.

The healthchecks.io heartbeat reports `/fail` when every check in a tick fails.
A banned IP and a quiet market look identical from the outside, so a tick where
nothing succeeded trips the same alarm as a crash.

## Not built yet

Playwright for JS-rendered sites. Auto-checkout is deliberately out of scope:
this notifies, it doesn't buy.
