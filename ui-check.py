#!/usr/bin/env python3
"""Layout invariants, checked in a real browser.

Not part of `pytest monitor/tests`, deliberately: the deploy gate runs the
suite on the server, where there is no Chromium, and a gate that cannot run is
a gate that blocks. Run this by hand after touching the dashboard.

    python3 ui-check.py

It seeds the rows that have actually broken the layout before, rather than
tidy ones. The bug it exists for: a watch status grew to "Watching · 739
tracked · 733 in the feed now", took its `auto` grid track with it, left the
name column about one character wide, and `overflow-wrap:anywhere` then broke
the name one letter per line down the whole screen. A 490px tall row that a
test asserting "the text is present" would have called fine.
"""
import json, os, pathlib, subprocess, sys, tempfile, threading, time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
os.environ.update(MONITOR_API_KEY="k", SESSION_SECRET="s" * 32,
                  TICK_SECONDS="3600")

WIDTHS = [("iphone-se", 375), ("iphone", 393), ("phone-max", 430),
          ("tablet", 820), ("wide", 1280)]
MAX_ROW_HEIGHT = 200       # a row taller than this is wrapping pathologically
MIN_TAP = 44               # px, the smallest comfortable target


def seed():
    from monitor import db, security
    from monitor.timeutil import stamp, utcnow
    from datetime import timedelta

    os.environ["MONITOR_PASSWORD_HASH"] = security.hash_password("pw")
    import importlib, monitor.config as cfg, monitor.security as sec
    importlib.reload(cfg); importlib.reload(sec)

    db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "ui.db"
    db.init_db()
    # A long unbroken handle and a long status, which is the pairing that broke.
    db.create_watch(
        name="ragtag-global.com · yohjiyamamotopourhomme",
        brand="ragtag-global.com", strategy="shopify", kind="collection",
        url="https://ragtag-global.com/collections/yohjiyamamotopourhomme",
        target_ref="yohjiyamamotopourhomme", currency="JPY",
        last_state="watching", last_checked_at=stamp(),
        baseline_json=json.dumps([f"p{i}" for i in range(403)]))
    wid = db.create_watch(
        name="satisfyrunning.com · all products", brand="satisfyrunning.com",
        strategy="shopify", kind="collection",
        url="https://www.satisfyrunning.com", last_state="watching",
        last_checked_at=stamp(), last_seen_count=733,
        baseline_json=json.dumps([f"s{i}" for i in range(739)]))
    for handle, yen, landed, star in [("a", 49160, 402, True),
                                      ("b", 88000, 719, False)]:
        db.insert_event(wid, "new_product", "watching", "watching", {
            "handles": [handle], "titles": {handle: "COMOLI Other"},
            "arrival": "new", "baseline_count": 739, "listed_ago_s": 210,
            "items": {handle: {"title": "COMOLI Other", "price": yen,
                               "landed": landed, "starred": star,
                               "url": f"https://x.com/products/{handle}"}}})
    return db


# A skip is a lie when someone is relying on the answer. Locally, no browser
# means "cannot check"; in CI it means the check did not happen while the job
# went green — which is the failure this whole file exists to catch, committed
# by the file itself. GitHub sets CI=true.
STRICT = bool(os.environ.get("CI") or os.environ.get("UI_CHECK_STRICT"))


def launch(pw):
    """Chromium, wherever this machine keeps it.

    The development container pins one at /opt/pw-browsers; a CI runner has
    Playwright install its own and expects to be left to find it. Hardcoding
    the first path made the second skip silently.
    """
    pinned = pathlib.Path("/opt/pw-browsers/chromium")
    if pinned.exists():
        print(f"  browser: {pinned}")
        return pw.chromium.launch(executable_path=str(pinned))
    print("  browser: playwright default")
    return pw.chromium.launch()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if STRICT:
            print("playwright is not installed, and CI must not pass without "
                  "running the check"); return 1
        print("playwright not installed — skipping"); return 0

    seed()
    import uvicorn
    from monitor.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8094,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2.5)

    failures = []
    checked = 0
    with sync_playwright() as pw:
        try:
            browser = launch(pw)
        except Exception as exc:
            if STRICT:
                print(f"could not start a browser: {exc}"); return 1
            print(f"could not start a browser ({exc}) — skipping"); return 0
        for label, width in WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            page.goto("http://127.0.0.1:8094/")
            page.wait_for_timeout(700)
            if page.locator("#pw").count():
                page.fill("#pw", "pw")
                page.click("text=SIGN IN")
                page.wait_for_timeout(1500)

            def fail(msg):
                failures.append(f"{label} ({width}px): {msg}")

            overflow = page.evaluate("document.documentElement.scrollWidth"
                                     " - document.documentElement.clientWidth")
            if overflow > 0:
                fail(f"page scrolls sideways by {overflow}px")

            for selector in (".wait", ".fired"):
                tall = page.eval_on_selector_all(
                    selector,
                    "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
                for height in tall:
                    if height > MAX_ROW_HEIGHT:
                        fail(f"{selector} row is {height}px tall — wrapping badly")

            body = page.inner_text("body")
            if "Invalid Date" in body:
                fail("'Invalid Date' rendered")
            if "NaN" in body or "undefined" in body:
                fail("'NaN' or 'undefined' rendered")

            if width <= 820:
                small = page.eval_on_selector_all(
                    ".x", "els => els.map(e => Math.round("
                          "Math.min(e.getBoundingClientRect().width,"
                          "e.getBoundingClientRect().height)))")
                for size in small:
                    if 0 < size < MIN_TAP:
                        fail(f"tap target {size}px, under {MIN_TAP}px")

            page.screenshot(path=f"/tmp/ui-{label}.png", full_page=True)
            checked += 1
            print(f"  {label:10} {width:>5}px  ok" if not failures
                  else f"  {label:10} {width:>5}px  checked")
            page.close(); ctx.close()
        browser.close()
    server.should_exit = True

    if not checked:
        print("\nFAILED: no width was actually measured")
        return 1

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  · {line}")
        return 1
    print("\nAll layout checks passed. Screenshots in /tmp/ui-*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
