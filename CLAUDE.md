# Restock Monitor — CLAUDE.md

## Project
Polls product pages and alerts on Telegram when something restocks or a new drop
lands. Targets: Shopify storefronts, Target (RedSky), Best Buy (developer API).

## Repo
https://github.com/christopher-hlee/restock-monitor

## Server
- Python 3.12, FastAPI, SQLite (WAL), APScheduler, httpx
- Venv: `source /home/platform/restock-monitor/monitor/venv/bin/activate`
- Start: `sudo systemctl restart monitor-api`
- Logs: `journalctl -u monitor-api -f`
- Port: 8003 (loopback only; Caddy terminates TLS in front)
- .env: `/home/platform/restock-monitor/monitor/.env`

## The invariant
**A failed check never becomes a stock state.** A 403, timeout, or parse error
leaves `last_state` untouched and increments `consecutive_failures`. Never
"simplify" this by writing `out_of_stock` on failure — it makes a dead monitor
indistinguishable from a quiet one. `monitor/tests/test_statemachine.py` pins it.

## Key files
- monitor/statemachine.py — pure transition logic, no I/O. Start here.
- monitor/scheduler.py — tick loop, tiering, jitter
- monitor/fetcher.py — shared httpx client, per-domain throttling, ETag
- monitor/strategies/ — one module per platform, each exposing `check` + `detect`
- monitor/db.py — schema and queries
- monitor/static/index.html — dashboard, single file

## Adding a strategy
1. New module in `monitor/strategies/` exposing `NAME`, `async check(watch)`,
   `async detect(url)`, both returning `CheckResult` / a config dict or None
2. Register it in `strategies/__init__.py` (`REGISTRY` and `DETECT_ORDER`)
3. Hostname-matched strategies go before Shopify in `DETECT_ORDER` — Shopify is
   the generic fallback and is identified by probing, not by domain
4. Tests with `respx`; never hit a real store from the suite

## Development rules
- Prefer structured endpoints (JSON, Atom, sitemap) over CSS selectors —
  selectors break silently on redesigns, which is the failure mode we design against.
- Keep polling polite: jitter, per-domain limits, honor Retry-After. Getting the
  IP banned is the real risk, not CPU.
- Never commit `.env`. Secrets are generated on the server.
- Run tests before pushing: `monitor/venv/bin/python -m pytest monitor/tests -c monitor/pytest.ini`
