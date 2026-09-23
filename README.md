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

A fired drop shows as one compact row per item — brand, name, price, a cart or
open link, and an `×` — grouped under **Just dropped**, because the watch itself
goes straight back to watching and would otherwise show nothing. Feed matches
land separately under **From your feeds**: an article is something to read, not
something to buy, and letting articles inflate "things to act on" is how that
number stops meaning anything.

Every row dismisses individually, each group clears at once, and dismissing
deletes the event server-side rather than setting a browser flag — this gets
read on a phone and on a laptop, and an alert cleared on one has to be gone on
the other. Anything still listed above is not repeated in **Recent alerts**,
which holds the rest of the history.

## A drop is a list; a restock is one transition

They alert differently on purpose. A restock is one item changing state, so it
gets one cart link per returned size. A drop is *n* items appearing at once, so
it gets one link per item — each pointing at that product, never at the
collection being polled. The handle in `products.json` is the store's own
canonical identifier, so `{store}/products/{handle}` needs no search and no
extra request, and the variants in that same response supply the cart
permalinks. A drop of exactly one item is treated as a restock and gets sizes.

## New means new to the STORE, not new to us

A drop watch used to alert on anything absent from its baseline. That is a fact
about our own memory and says nothing about the shop: a catalogue larger than
one page, a collection that re-sorts, or a widened sweep all put
previously-unseen products in front of us, and every one of them fired. One
reported item had been on sale for 329 days.

So the baseline is now a **dedupe ledger, not a trigger**. Each unseen product
is classified by the store's own timestamps:

| `published_at` | `created_at` | outcome |
|---|---|---|
| since the last sweep | since the last sweep | **new release** — alert |
| since the last sweep | long ago | **relisted** — alert, labelled |
| before the last sweep | — | absorbed silently |
| absent | absent | alert, labelled *unverified* |

The window runs from the last sweep that actually **read** the catalogue, not
the last check — after a three-day outage the latter is minutes old and would
hide every release missed during it.

Store timestamps go through `parse_instant`, never `parse`. `parse` truncates at
19 characters for our own SQLite stamps, which throws away a timezone offset:
Shopify serves `published_at` in the shop's own timezone, so a product listed
this minute by a New York store read as four hours old, fell outside the window
and was absorbed in silence. Permanently. Anything arriving from a store has to
be normalised to UTC first.

A conditional request is only sent when the catalogue fits in one page. An ETag
validates page one, not the shop: with 333 products a new item can land on page
two while page one is byte-identical, and a 304 would end the sweep before
page two was ever fetched — while still advancing the window, so the item reads
as old whenever it finally becomes visible.

Keeping `published_at` and `created_at` apart is what makes a relist legible:
Shopify resets the publish date when an item is unpublished and put back, so a
years-old product returning looks brand new by that field alone. Collapsing the
two with `or` is exactly what reported a sold-out shirt as "listed 6m ago".

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
| fast | 35 s     | only while armed for a known drop window |

Worst case from a product appearing to the phone buzzing is **50s**: 42s of
poll gap (35s floor plus 20% jitter), 5s of scheduler granularity, and a few
seconds of fetch and delivery. A test asserts that budget in seconds, so a
future change to either number fails there rather than during a drop.

`TICK_SECONDS` is clamped to 8s no matter what `.env` asks for, and the budget
is asserted against that ceiling rather than the running value. A test that
reads live config asserts a property of whoever ran it: it passed on a laptop
with no `.env` and failed on the box, whose `.env` still pinned the old 15s —
the promise above is not something a deployment setting gets to revoke quietly.

### "Coming soon" is a drop that hasn't happened yet

A brand publishes a product, puts it in the collection, and leaves every
variant unbuyable for days. Detecting new *handles* is blind to what happens
next: by the time the button goes live the catalogue has not grown, so there is
nothing new to notice and the release passes in silence.

So a collection watch tracks two different things. **Has the catalogue grown**
answers "something appeared" — that is the 🆕 alert, and it fires for a
coming-soon listing too, because knowing a drop is scheduled is worth knowing.
**Can I buy it now** answers "the drop is happening" — that is the ⚡ *now
buyable* alert, fired on a recorded unbuyable → buyable flip, and it is the one
that arrives with a working cart link.

Three rules keep it from crying wolf:

- **A flip needs a recorded `false` to flip from.** "Available, and we have no
  record" is not a launch. Otherwise the first sweep after this shipped would
  have announced the entire catalogue.
- **Absence is not unavailability.** A handle missing from a sweep is left
  exactly as it was. A page cut short by a rate limit would otherwise record
  the whole tail as unavailable and then announce a launch for every one of
  them on the next full read — the same mistake as reading a 403 as sold out.
- **One release is one alert.** Sizes sell out and come back all through a busy
  drop; a launch is not repeated for the same product within an hour.

### Why didn't this alert?

Silence is ambiguous. A product that never alerted might be invisible to us,
might have been absorbed as already-on-the-shelf, or might be sitting in the
ledger waiting to become buyable — and from the outside those are identical.
**Why no alert?** on the dashboard takes a product URL and says which one it
is, and what would happen next. `GET /api/inspect?url=…` is the same answer as
JSON. It re-reads the watch's own feed rather than trusting the baseline,
because "not in our baseline" means either *not swept yet* or *this collection
will never contain it* — and only the second is a reason to watch something
else.

It reports three sources separately — the catalogue feed we poll, the product
API, and the product page's schema.org — and **never fills in one that said
nothing**. An early version read availability from an endpoint that does not
carry the flag, and a default meant for building cart links turned that silence
into "buyable now" about a product the storefront was showing as Coming Soon.
Silence is now reported as silence. When the page and the catalogue disagree,
the panel says so outright: the storefront is what a person sees, so a page
saying *not purchasable* over an API saying *buyable* means the store signals
"coming soon" somewhere other than the variant flag we watch — which is
something to be told, not something to resolve quietly in the API's favour.

### A partial read is not a sweep

The catalogue is paged, and paging stops for two very different reasons: the
store ran out of products, or we ran out of permission to keep asking (our own
page cap, or a 429). Calling the second one a complete sweep loses drops in
silence — the tail we never fetched stays out of the baseline while the sweep
window advances past it, so those products read as *old* whenever they finally
come into view. Same failure as a 304 answering for a page never requested.

A truncated read is therefore marked, and a marked read does not stamp
`last_sweep_at`. The window stays open until a read that actually finished.

The page cap is ten pages, 2500 products. It only engages while pages keep
coming back full, so an ordinary catalogue still costs two or three requests.
It was four pages — a silent 1000-product ceiling — with a catalogue already at
717.

### A record being drafted is not a listing

A brand assembles a drop in public. Satisfy creates the products days ahead —
published, so they appear in the feed — with no price, no photography, no
sizes, and working titles like *moth pred women* or *MothTech™ T-ShirtCL*. One
handle carried a `-13` suffix: Shopify numbering the thirteenth attempt at the
same one as it was made and deleted over and over. Two of the pages we linked
had 404'd by the time they were opened.

Every one of those is a genuinely new handle published seconds ago, so every
one is a correctly classified `new` arrival — and five in a morning is how an
alert becomes wallpaper.

The signal that separates them is **price**. A finished "coming soon" listing
has one: 260 USD, photographed, sized, just not purchasable yet. A record
still being drafted has 0 or none. So an arrival with a stated price of zero
and nothing on sale stays silent.

This needed a fix upstream first: `price` was read from the *buyable* variants
only, which makes it `None` for anything sold out or unreleased — so a
finished coming-soon listing and a half-built record looked identical. The
asking price is now read across all variants, independent of availability.

Two guards, for the same reason as everywhere else: a product with **no
variants** tells us nothing about price, and the Atom fallback carries no
prices at all. Neither is read as "priced at zero", or every store behind a
JSON gate goes quiet.

And as ever, silence is safe only because nothing is forgotten: the record
enters the baseline and the ledger, so when it acquires a price and a variant
on sale it announces itself as a **release**.

### "Coming Soon" and "Notify Me When Available" are one flag

The storefront distinguishes them plainly — one product has not been released,
the other has sold out — and a release matters more than a restock. The
catalogue API collapses both into `available: false`, so nothing in the feed
tells them apart.

Our own history does. The availability ledger records `ever_buyable` the first
time a product is seen on sale, and it survives the product selling out. So a
product going buyable is a **release** if we have never once seen it on sale,
and a **restock** if we have:

| storefront | ledger | alert |
|---|---|---|
| Coming Soon | never seen on sale | ⚡ *released* |
| Notify Me When Available | seen on sale before | 🔄 *back in stock* |

The wording is "on sale for the first time **since I started watching**",
because that is the honest extent of the claim: we cannot see what the store
sold before the watch existed. A product first met while already sold out will
read as a release when it returns, and saying so is better than asserting a
debut we have no way to know about.

Watch rows show **Not yet on sale** rather than *Sold out* for a product with
no recorded sale, for the same reason.

### A republished item you cannot buy is not news

Watching the whole store instead of a curated collection costs something: the
store-wide feed carries the archive, and brands put old stock back through it
routinely. Every one of those looks like an arrival, and alerting on them is
how a useful notification becomes one you swipe away unread.

So an arrival only speaks up if you could act on it. A **relisted** or
**undateable** product with no buyable variant is recorded silently; a
genuinely **new** listing still announces itself even when it is not yet
buyable, because knowing a drop is coming is the point of a "coming soon" page.

Staying quiet costs nothing, and that is the part that makes this safe: the
product still enters the baseline and the availability ledger, so it is armed.
The moment a variant goes on sale it announces itself as a launch — which was
the alert worth having all along.

One exception, and it is load-bearing: a store we can only read through the
Atom fallback sends no availability at all. Reading that silence as "not
buyable" would mute every relisted arrival from such a store, permanently and
invisibly, so an unstated flag is never treated as a no.

### "717 tracked" is memory, not inventory

The baseline is a union and never shrinks: a product the store retires stays
counted, on purpose, so that something briefly dropping out of the feed cannot
re-alert when it returns. That makes the tracked figure drift above what is
actually on sale. Watch rows show the live count beside it whenever the two
differ — *717 tracked · 690 in the feed now*. Neither number can contain a
duplicate: handles are de-duplicated per read and the baseline is a set.

### Watch the store, not the collections

If a brand adds a drop to `/collections/new-arrivals` but not to `shop-all`,
the fix is **not** a watch per collection. That multiplies traffic to a store
that already rate-limits us — the thing that got a watch auto-paused once —
and still misses whichever collection you did not think of.

A Shopify store's `/products.json` is every product published to the online
store, regardless of collection: a superset of every collection, read in one
request cycle. Paste the bare store URL (no `/collections/` path) into **Add a
watch** and the watch covers the whole catalogue. Up to 1000 products, four
pages of 250.

The tier is a starting point, not a setting: each watch **learns** its own
cadence. Every clean check earns a little speed (−15s); a 429 gives it back
multiplicatively, honouring `Retry-After` when the store sends one. Clamped to
35s–15min. This is additive-increase/multiplicative-decrease, the shape TCP uses,
and it exists because a fixed number is wrong for every store but one —
hardcoding a fast interval got a watch rate-limited and auto-paused, while five minutes let a
drop be missed by sixteen minutes.

A 429 costs at most two extra minutes. It used to **double** the interval, which
at the five-minute tier meant a ten-minute blind spot: the store asked us to slow
down and we heard "stop".

Three more details do the real work:

- **Jitter** (±20%) — a perfectly periodic request pattern is a signature.
- **ETag / If-None-Match** — most polls come back `304`, cheap and low-profile.
- **Arming** — `POST /api/watches/{id}/arm` switches to the fast tier for a
  set window. Arming never teaches the controller: a temporary cadence must not
  become permanent.

### Measuring the lag

`products.json` carries `published_at`, so every drop alert reports how far
behind the store it was — "listed 47s before this alert", amber past five
minutes. Without it, "the alert was late" is an argument; with it, it is a
reading that separates our polling lag from a stale CDN document. An unmeasurable
lag prints nothing rather than zero.

## Strategies

Auto-detected from the URL you paste — you never pick one.

- **`shopify`** — `/products/{handle}.js` for availability, `/products.json` for
  new-handle detection, Atom feed as fallback when JSON is gated. Alerts carry a
  `/cart/{variant_id}:1` permalink that drops the item straight into your cart.
- **`target`** — RedSky, including per-store pickup quantities. Local pickup is
  far less contested than online stock.
- **`bestbuy`** — the official developer API. Needs a free `BESTBUY_API_KEY`.

- **`retail`** — reads the schema.org JSON-LD that retailers embed for Google
  Shopping. A documented public standard rather than a private API scraped from
  a site's own frontend, which is the distinction that killed the Target
  strategy when RedSky retired its endpoint. Handles the storefronts nothing
  else understands — Nintendo, GameStop, most of the rest — and carries
  `PreOrder`, so "available at a later date" flipping to "preorder now" is a
  state change it can see. **Tried last on purpose**: Shopify pages publish
  JSON-LD too, and the Shopify strategy is strictly better for them.

  A page it cannot parse reports `failing` with a specific reason, never
  "sold out". An unreadable page says nothing about stock, and claiming
  otherwise would look identical to the truth for the life of the watch.

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

## Auto-deploy

Run once on the server, as `platform`:

```bash
cd ~/restock-monitor && ./bootstrap-autodeploy.sh
```

A systemd timer then checks the tracked branch every five minutes and deploys
new commits on its own, so shipping a change is just a push.

`autodeploy.sh` is not `deploy-monitor.sh`. That one is the installer — it
apt-installs Caddy and finishes by probing live Shopify, which is right once and
far too heavy every five minutes. The updater is the narrow path, and it is
built around a single idea: **an auto-deploy that can ship broken code to a
monitor is worse than no auto-deploy.** The reason this app exists is to catch a
drop you'd otherwise miss, so quietly swapping a working monitor for a broken
one defeats it. Therefore:

- Nothing new? Exit silently. A chatty timer is a timer you learn to ignore.
- Tests run **before** anything restarts. They fail, the tree reverts and the
  running service is never touched.
- The restart doesn't come up healthy? Roll back and restart again — including
  reinstalling the previous dependencies, since new packages may be the very
  thing that broke it.
- Telegram only on a real deploy, a block, or a rollback — and only **once**
  per commit. A failure resets the tree, so the next tick would otherwise see
  the same commit as new and re-alert every five minutes forever.
- "Cannot run the tests" and "the tests failed" are reported as different
  things, because one means fix the code and the other means fix the box.

The test runner lives in `requirements.txt`, not in a dev-only file. That is
deliberate: the gate runs the suite **on the server**, so pytest is a production
dependency of this deployment, and pretending otherwise is what took the gate
down once already — the box had no pytest, so "cannot run tests" was reported as
"tests failed". Every test monkeypatches `DB_PATH` to a temp file, so running
the suite on the box never touches live data.

```
systemctl list-timers restock-autodeploy     # when it next runs
journalctl -u restock-autodeploy -f          # what it did
sudo systemctl start restock-autodeploy      # deploy now
sudo systemctl disable --now restock-autodeploy.timer   # turn it off
```

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

## Signing in from the chat

A password needs somewhere to type it. A corporate network that routes
unrecognised domains through browser isolation streams the dashboard as pixels
and blocks text input, so the password field is unusable while everything
behind it runs fine. The bot is reachable because it is not a browser.

Send **`/login`** and tap the link. It is signed, expires in ten minutes, and
is good exactly once.

- The nonce is **claimed** with a conditional delete, not read-then-deleted. A
  wrong value leaves the live token alone — otherwise anyone reaching the
  endpoint could lock you out by opening it with nonsense, and an old link
  re-tapped from the chat log would do it by accident.
- Minting a new link invalidates the previous one; at most one is ever live.
- Only the configured chat is obeyed. Anyone can message a bot whose username
  they know, so every other update is dropped in silence rather than answered
  with "not authorised", which would confirm the bot is live.

Other commands: **`/check <url>`** reports what a watch on that address would
actually see — platform, product count, whether it is too large to read in one
sweep, and any saved-search parameters it would have to apply itself. That is
the question that otherwise needs a shell on the server, which is exactly what
is unavailable when a network sits between you and the dashboard.
**`/status`** lists watches and when each was last swept.

Long polling, not a webhook: a webhook needs a public URL registered with
Telegram and a secret in the environment, and editing the environment on the
server is the thing that is hard to reach. The bot token is already there.
The app learns its own public address from the first inbound request, since it
listens on localhost behind a proxy and the hostname lives in the proxy config
(`PUBLIC_URL` overrides).

## Adding a store whose filters are not Shopify's

Some stores run faceted search through a third party — RAGTAG Global runs Boost
AI Search & Discovery — and encode a saved search in the storefront URL:

```
/collections/men_all?pf_st_availability=in-stock
  &pf_t_gender=gender_Mens&pf_t_size=size_L&pf_t_size=size_XL
  &pf_v_brand=COMOLI
```

A **collection page is not a product**, and the JSON-LD reader used to think
otherwise. Every product card in a grid emits its own schema.org `Product`
block, so `havenshop.com/collections/auralee` detected as *"Ultra Fine Tropical
Wool Zip Blouson Top Charcoal"* — whichever garment happened to sort first.
The watch would have monitored that one item while its owner believed it was
watching the brand. The address is now checked before the page is fetched (no
answer a collection page could give would make it a product), and a page
declaring itself a `CollectionPage` is refused outright. `/collections/x/
products/y` is still a product, because it is one.

A **search results page** is refused outright. `/search?q=auralee` is a fine
way for a person to find a brand and a trap for a monitor: Shopify's search is
not exposed through the API, so a watch built from it polls the whole
catalogue and ignores the term — returning products, reporting healthy, and
answering a different question than the one asked. Use `/collections/<handle>`,
or watch a collection and filter by vendor.

**Those parameters have no effect on `products.json`.** Handing the URL to
Shopify returns the whole unfiltered collection — which looks like it is
working while being wrong, the worst failure available. So the filter is
re-implemented on our side: `filters.parse_boost_url` turns the URL into
predicates, and `filters.matches` evaluates them against each entry.

Repeating one facet is an **or**; separate facets are an **and**.
`pf_t_size=size_L&pf_t_size=size_XL` means L or XL, and flattening the facets
into a single required-tags list would demand a garment be both, which nothing
is.

The filter is applied **at the point of speaking, never to the sweep**.
Everything the collection holds still enters the baseline and the ledger, so
narrowing or widening a saved search later cannot replay a catalogue you have
already been shown — and a garment listed three weeks ago does not become new
because you started wanting its size today.

### Consignment stores invert the event

A manufacturer's product has many sizes and gets replenished, so a restock is
`available` flipping false to true on a known variant. A consignment listing is
a single unique garment, quantity one, one variant titled `Default Title`, gone
for good when sold. Nothing ever restocks; the only event is *a new listing
matching my filter*. That is the detection this app already does — a
collection watch diffs set membership — pointed at a store whose metadata lives
somewhere else.

Somewhere else being **tags**. Size on such a store is `size_M`, not
`variant.title`, so every variant-reading size path matches zero items without
erroring. `size_pref` is for manufacturer stores; `filter_json` is for these.

### A star, not a grade

The bottleneck was never finding listings — one filtered wishlist had seventy.
It was evaluating them one at a time. So a listing that clears the cheap gates
gets a **★** and a landed cost, and everything else stays quiet.

`/star <id> colors=black,navy max=450 fx=142 condition=A`

**Landed cost** is what the thing costs to have: sticker, converted, plus duty.
De minimis was suspended in June 2026, so duty is not optional and the listed
price is never what is paid. Shipping is free worldwide at any order size, so
it drops out. The multiplier keys off the country-of-origin tag — Japan 1.16,
China 1.30 — and the rate is **printed in the alert**, because an estimate
whose assumptions are invisible is worse than none. A missing FX rate produces
no number rather than a wrong one.

The gates are tag-only and cheap, which is the point: they run on every
listing and, in practice, eliminate most of them.

- **colour** — a discount on something that will not be worn is waste, not
  saving
- **landed under your ceiling** — no price is not "under the ceiling"
- **condition, read against the category** — the rank alone means nothing.
  Seat and hem go first, so trousers want rank A where a shirt is fine at B;
  fine knits pill where a shell only scuffs

**What the star cannot see, and does not claim to.** The full method anchors
every judgement to what the same money buys delivered to the US today: new
retail, used comps, and whatever sale the brand is running this week. None of
those are in a catalogue feed. So ★ means *this one survived the cheap gates
and is worth pricing against US retail* — the step that is still yours. It
will star things the full method would skip: it cannot see that a listing sits
above its own new price, and it cannot see that you already own three knits.

One rule it exists to obey: **never compare a RAGTAG price to another RAGTAG
price.** The catalogue is internally consistent and externally mispriced —
roughly a third of listings sit above US new — so a percentile against the
store's own history would be a confident number measuring nothing.

Material would sharpen the duty estimate by up to seventeen points, enough to
flip marginal calls, but it lives in the product page's spec table rather than
the feed. Today's estimate is the flat per-origin rate, and says so.

### Currency

Per watch, defaulting to USD. A yen store read as dollars is wrong by a factor
of about 142, in the flattering direction. There is no FX conversion: the
alert prints ¥49,160 because that is what the store charges, and inventing a
dollar figure would mean carrying a rate that goes stale silently.

