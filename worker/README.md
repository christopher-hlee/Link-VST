# Egress proxy (Cloudflare Worker)

Optional. Deploy this only once `verify-targets.sh` shows a strategy being
refused from the VPS — it exists to give the monitor a second network identity
for those specific hosts.

Retailer bot protection scores datacenter IP ranges harshly. A request that gets
a `403` from your VPS often succeeds unchanged from elsewhere, and Cloudflare's
edge is elsewhere. The free plan's 100k requests/day is far more than this
workload needs.

## Deploy

```bash
npm install -g wrangler
wrangler login
cd worker

# Restrict it to the hosts you actually need before deploying.
$EDITOR wrangler.toml          # ALLOWED_HOSTS

wrangler secret put PROXY_KEY  # paste a long random string
wrangler deploy
```

Then on the VPS, in `monitor/.env`:

```
PROXY_URL=https://restock-monitor-proxy.<your-subdomain>.workers.dev
PROXY_KEY=<the same string>
PROXY_HOSTS=redsky.target.com,api.bestbuy.com
```

`PROXY_HOSTS` is the monitor's own routing list: only requests to those hosts go
through the Worker, everything else goes direct. Add a host here *and* to
`ALLOWED_HOSTS` above — the monitor decides what to route, the Worker decides
what it is willing to fetch, and both have to agree.

Restart, then re-run `./verify-targets.sh` to confirm the previously blocked
strategy now answers.

## Why it is locked down

An open URL that fetches arbitrary sites gets discovered and abused, so the
Worker requires both a shared key and an allowlisted destination. Neither alone
is sufficient. It proxies `GET` only, refuses non-HTTPS targets, caps responses
at 5 MB, and drops inbound cookies and authorization headers rather than
forwarding them.

It passes `ETag`/`If-None-Match` through in both directions and preserves `304`
responses intact. Losing that would turn every cheap conditional poll into a
full download — more bandwidth, and a much more conspicuous traffic pattern.

## Limits

Cloudflare egress is still shared infrastructure, not a residential address. A
site that blocks *all* non-residential traffic will block this too. If that
happens the remaining option is running the fetcher from a home connection — a
Raspberry Pi over Tailscale — with the VPS keeping the dashboard and database.
