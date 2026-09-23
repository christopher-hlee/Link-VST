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

Since the phone redesign it also drives the page: opens a drop bucket, checks
the sheet fits the screen and scrolls inside itself, checks the photos load and
the dock never covers content, and dismisses one product to prove its siblings
from the same sweep survive.
"""
import json, os, pathlib, struct, sys, tempfile, threading, time, zlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
# No polling. Setting TICK_SECONDS high does not work — it is clamped to 8s
# on purpose — so the scheduler has to be switched off outright, or rendering
# a page means hitting the shops this monitor watches from whatever machine
# happens to be running the check.
os.environ.update(MONITOR_API_KEY="k", SESSION_SECRET="s" * 32,
                  MONITOR_NO_SCHEDULER="1")

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
        target_ref="yohjiyamamotopourhomme",
        last_state="watching", last_checked_at=stamp(),
        baseline_json=json.dumps([f"p{i}" for i in range(403)]))
    wid = db.create_watch(
        name="satisfyrunning.com · all products", brand="satisfyrunning.com",
        strategy="shopify", kind="collection",
        url="https://www.satisfyrunning.com", last_state="watching",
        last_checked_at=stamp(), last_seen_count=733,
        baseline_json=json.dumps([f"s{i}" for i in range(739)]))
    # RAGTAG: consignment items going on sale a few at a time, several per
    # sweep, some starred. Twelve, so the sheet has to scroll. The feed is
    # priced in dollars, as RAGTAG's actually is when read from a US server.
    ragtag = db.create_watch(
        name="ragtag-global.com · comoli", brand="ragtag-global.com",
        strategy="shopify", kind="collection",
        url="https://ragtag-global.com/collections/comoli",
        last_state="watching", last_checked_at=stamp(),
        baseline_json=json.dumps([f"c{i}" for i in range(182)]))
    sweeps = [["c1", "c2", "c3"], ["c4", "c5", "c6", "c7"], ["c8"],
              ["c9", "c10", "c11", "c12"]]
    for n, handles in enumerate(sweeps):
        items = {}
        for h in handles:
            i = int(h[1:])
            items[h] = {
                "title": ("COMOLI Tielocken Coat Wool Gabardine Navy Size 2 "
                          "— Excellent Condition (A)") if i == 1 else f"COMOLI Shirt Jacket {i}",
                "price": 575.0 + i * 10, "available": True,
                "available_stated": True,
                "image": None if i == 5 else f"https://img.test/c{i}.jpg",
                "url": f"https://ragtag-global.com/products/{h}",
                "offers": [{"title": "Default Title", "cart_url":
                            f"https://ragtag-global.com/cart/{i}:1"}]}
            if i in (1, 4):
                items[h].update(landed=402 + i, starred=True)
        db.insert_event(ragtag, "new_product", "watching", "watching", {
            "handles": handles, "items": items, "arrival": "launched",
            "first_sale": handles[:1], "baseline_count": 182})

    # Satisfy: a release, a coming-soon listing in the same sweep, a relist.
    for handles, arrival in [(["heatcrush-desert-shorts-moonstruck",
                               "mothtech™-t-shirt-cl"], "new"),
                             (["rippy-3-trail-shorts-aged-black"], "relisted")]:
        items = {h: {"title": h.replace("-", " ").title(), "price": 185.0,
                     "available": h != "mothtech™-t-shirt-cl",
                     "available_stated": True,
                     "image": f"https://img.test/{len(h)}.jpg",
                     "url": f"https://www.satisfyrunning.com/products/{h}",
                     "offers": [{"title": "S", "cart_url": "https://x/cart/1:1"},
                                {"title": "M", "cart_url": "https://x/cart/2:1"}]}
                 for h in handles}
        db.insert_event(wid, "new_product", "watching", "watching", {
            "handles": handles, "items": items, "arrival": arrival,
            "baseline_count": 739, "listed_ago_s": 210})

    # A news feed, grouped the same way but read rather than bought.
    feed = db.create_watch(
        name="Nintendo Life · zelda", brand="Nintendo Life", strategy="announce",
        kind="collection", url="https://www.nintendolife.com/feeds/latest",
        target_ref="zelda", last_state="watching", last_checked_at=stamp())
    db.insert_event(feed, "new_product", "watching", "watching", {
        "is_announcement": True, "handles": ["n1"],
        "titles": {"n1": "Zelda: Ocarina of Time remake confirmed for Switch 2"},
        "links": {"n1": "https://www.nintendolife.com/news/zelda"}})

    # One of each product row: buyable in sizes, held, and broken.
    db.create_watch(
        name="PeaceShell Climb Pants", brand="Satisfy", strategy="shopify",
        kind="product", url="https://www.satisfyrunning.com/products/climb",
        target_ref="climb", last_state="in_stock", last_price=345.0,
        last_title="PeaceShell Climb Pants", last_image="https://img.test/pants.jpg",
        last_checked_at=stamp(), size_pref="m",
        last_offers_json=json.dumps([
            {"title": t, "cart_url": f"https://x/cart/{t}:1", "preferred": t == "M"}
            for t in ("S", "M", "L", "XL")]))
    db.create_watch(
        name="MothTech Tee", brand="Satisfy", strategy="shopify", kind="product",
        url="https://www.satisfyrunning.com/products/tee", target_ref="tee",
        last_state="held", last_price=110.0, last_title="MothTech Tee",
        last_checked_at=stamp(), size_pref="m",
        last_offers_json=json.dumps([{"title": "L", "cart_url": "https://x/c/L:1"}]))
    db.create_watch(
        name="Havenshop · auralee", brand="havenshop.com", strategy="shopify",
        kind="collection", url="https://havenshop.com/collections/auralee",
        last_state="watching", consecutive_failures=3, last_checked_at=stamp(),
        last_error="HTTP 403 from https://havenshop.com/collections/auralee/products.json")
    db.insert_event(wid, "restock", "out_of_stock", "in_stock", {"title": "Old tee"})
    return db


def png(w=8, h=10, rgb=(150, 110, 80)) -> bytes:
    """A tiny portrait PNG, so object-fit has something to get wrong."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Heights past which a row is wrapping pathologically. Product rows carry an
# image and a row of size chips on a phone, so they get more room.
ROW_LIMITS = {".wait": MAX_ROW_HEIGHT, ".fired": MAX_ROW_HEIGHT,
              ".bucket": 170, ".feed": 160, ".row": 420}


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
        image = png()
        for label, width in WIDTHS:
            phone = width <= 820
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            ctx.route("https://img.test/**", lambda route: route.fulfill(
                status=200, content_type="image/png", body=image))
            page = ctx.new_page()
            page.on("dialog", lambda d: d.dismiss())
            # A script error can leave a layout that measures fine and does
            # nothing when tapped.
            page.on("pageerror", lambda exc, label=label, width=width:
                    failures.append(f"{label} ({width}px): script error: {exc}"))
            page.goto("http://127.0.0.1:8094/")
            page.wait_for_timeout(700)
            if page.locator("#pw").count():
                page.fill("#pw", "pw")
                page.click("text=SIGN IN")
                page.wait_for_timeout(1500)

            def fail(msg):
                failures.append(f"{label} ({width}px): {msg}")

            def no_junk(where):
                text = page.inner_text(where)
                if "Invalid Date" in text:
                    fail(f"'Invalid Date' rendered in {where}")
                if "NaN" in text or "undefined" in text:
                    fail(f"'NaN' or 'undefined' rendered in {where}")

            overflow = page.evaluate("document.documentElement.scrollWidth"
                                     " - document.documentElement.clientWidth")
            if overflow > 0:
                fail(f"page scrolls sideways by {overflow}px")

            for selector, limit in ROW_LIMITS.items():
                tall = page.eval_on_selector_all(
                    selector,
                    "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
                for height in tall:
                    if height > limit:
                        fail(f"{selector} row is {height}px tall — wrapping badly")

            no_junk("body")

            # Drops are grouped: one row per watch, never one per product.
            buckets = page.locator(".bucket").count()
            if buckets != 3:
                fail(f"expected 3 drop buckets (RAGTAG, Satisfy, feed), got {buckets}")
            ragtag_row = page.locator(".bucket", has_text="comoli")
            first = ragtag_row.inner_text() if ragtag_row.count() else ""
            if "12 buyable items" not in first:
                fail(f"RAGTAG bucket should say '12 buyable items': {first!r}")
            satisfy_row = page.locator(".bucket", has_text="satisfyrunning")
            satisfy = satisfy_row.inner_text() if satisfy_row.count() else ""
            if "3 items · 2 buyable" not in satisfy:
                fail(f"a coming-soon item must not be counted buyable: {satisfy!r}")

            # Photos arrive, and are squares whatever shape the file is.
            page.wait_for_timeout(300)
            shots = page.eval_on_selector_all(".bucket img", """els => els.map(e => ({
                ok: e.complete && e.naturalWidth > 0,
                w: Math.round(e.getBoundingClientRect().width),
                h: Math.round(e.getBoundingClientRect().height)}))""")
            if not shots:
                fail("no product photos rendered in the drop buckets")
            for shot in shots:
                if not shot["ok"]:
                    fail("a product photo failed to load")
                elif shot["w"] != shot["h"]:
                    fail(f"photo drawn {shot['w']}x{shot['h']}, not square")

            # Header figures: every label whole, none clipped to an ellipsis.
            clipped = page.eval_on_selector_all(
                ".stat span", "els => els.filter(e => e.scrollWidth > e.clientWidth)"
                              ".map(e => e.textContent)")
            if clipped:
                fail(f"header labels clipped: {clipped}")

            dock = page.locator(".dock")
            if phone:
                if not dock.is_visible():
                    fail("the phone dock is missing")
                else:
                    # Nothing may end up underneath it once scrolled to the end.
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(150)
                    hidden = page.evaluate("""() => {
                        const top = document.querySelector('.dock').getBoundingClientRect().top;
                        const last = document.getElementById('build')
                            || document.querySelector('#sheet > :last-child');
                        return last ? Math.round(last.getBoundingClientRect().bottom - top) : 0;
                    }""")
                    if hidden > 0:
                        fail(f"the dock covers the last {hidden}px of the page")
                    page.evaluate("window.scrollTo(0, 0)")
            elif dock.is_visible():
                fail("the phone dock is showing on a wide screen")

            if phone:
                tiny = page.evaluate("""() => {
                    const out = [];
                    const sel = '.x, .bucket, .dock button, .item-acts a, .item-acts button, .btn-text';
                    for (const e of document.querySelectorAll(sel)) {
                        const r = e.getBoundingClientRect();
                        if (!r.width || !r.height) continue;
                        const text = e.matches('.btn-text');
                        const side = text ? r.height : Math.min(r.width, r.height);
                        if (side < 44) out.push((e.className || e.tagName) + ' ' + Math.round(side));
                    }
                    return out;
                }""")
                for t in tiny:
                    fail(f"tap target under {MIN_TAP}px: {t}")

            page.screenshot(path=f"/tmp/ui-{label}.png", full_page=True)

            # ---- the bucket sheet
            page.locator(".bucket", has_text="comoli").click()
            page.wait_for_timeout(250)
            sheet = page.locator(".bucket-sheet")
            if not sheet.is_visible():
                fail("tapping a bucket did not open its sheet")
            else:
                items = page.locator(".bucket-sheet .item").count()
                if items != 12:
                    fail(f"the RAGTAG sheet lists {items} products, not 12")
                geo = page.evaluate("""() => {
                    const s = document.querySelector('.bucket-sheet');
                    const r = s.getBoundingClientRect();
                    return {top: r.top, bottom: r.bottom, vh: innerHeight,
                            scrolls: s.scrollHeight > s.clientHeight + 1,
                            locked: document.body.classList.contains('locked'),
                            wide: [...s.querySelectorAll('.item')]
                                    .filter(e => e.scrollWidth > e.clientWidth + 1).length};
                }""")
                if geo["top"] < -1 or geo["bottom"] > geo["vh"] + 1:
                    fail(f"the sheet does not fit the screen: {geo}")
                if not geo["scrolls"]:
                    fail("twelve products and the sheet does not scroll")
                if not geo["locked"]:
                    fail("the page behind an open sheet can still scroll")
                if geo["wide"]:
                    fail(f"{geo['wide']} sheet rows overflow sideways")
                for height in page.eval_on_selector_all(
                        ".bucket-sheet .item",
                        "els => els.map(e => Math.round(e.getBoundingClientRect().height))"):
                    if height > 260:
                        fail(f"a sheet row is {height}px tall — wrapping badly")
                no_junk(".bucket-sheet")
                # The heading and the close stay put while the list scrolls.
                page.evaluate("document.querySelector('.bucket-sheet').scrollTop = 1e6")
                page.wait_for_timeout(150)
                stuck = page.evaluate("""() => {
                    const s = document.querySelector('.bucket-sheet').getBoundingClientRect();
                    const h = document.querySelector('.sheet-head').getBoundingClientRect();
                    return Math.abs(h.top - s.top) <= 1;
                }""")
                if not stuck:
                    fail("the sheet heading scrolled away with the list")
                # innerText applies text-transform, so the tags read in capitals.
                words = page.inner_text(".bucket-sheet").lower()
                if "released" not in words or "back in stock" not in words:
                    fail("launched items lost their released / back-in-stock tags")
                page.screenshot(path=f"/tmp/ui-{label}-sheet.png")
                page.click(".sheet-head .x")
                page.wait_for_timeout(150)
                if sheet.is_visible():
                    fail("the sheet's close button did not close it")

            # ---- the add sheet: no iOS zoom, and its button in reach
            (page.locator(".dock .btn-accent") if phone
             else page.locator(".hdr-actions button", has_text="Add a watch")).click()
            page.wait_for_timeout(250)
            size = page.evaluate("parseFloat(getComputedStyle(document.getElementById('aUrl')).fontSize)")
            if phone and size < 16:
                fail(f"inputs are {size}px — iOS will zoom the page on focus")
            bottom = page.evaluate("document.getElementById('saveBtn').getBoundingClientRect().bottom")
            if bottom > 900:
                fail("'Start watching' is below the bottom of the screen")
            if phone:
                for h in page.eval_on_selector_all(
                        "#add .seg button",
                        "els => els.map(e => Math.round(e.getBoundingClientRect().height))"):
                    if h < MIN_TAP:
                        fail(f"a segmented button is {h}px tall, under {MIN_TAP}px")
            no_junk("#add")
            page.keyboard.press("Escape")


            # ---- light / dark: reachable, working, legible, remembered
            toggle = page.locator(".dock .theme" if phone else ".hdr-actions .theme")
            if not toggle.is_visible():
                fail("the light/dark button is not on screen")
            else:
                box = toggle.bounding_box()
                if min(box["width"], box["height"]) < MIN_TAP:
                    fail(f"the light/dark button is {min(box['width'], box['height']):.0f}px")
                cramped = page.eval_on_selector_all(
                    ".dock button", "els => els.filter(e => e.offsetParent &&"
                    " e.scrollWidth > e.clientWidth + 1).map(e => e.textContent.trim())")
                if cramped:
                    fail(f"dock labels do not fit: {cramped}")
                toggle.click()
                page.wait_for_timeout(200)
                theme = page.evaluate("document.documentElement.dataset.theme || 'dark'")
                ground = page.evaluate("getComputedStyle(document.body).backgroundColor")
                if theme != "light" or ground == "rgb(16, 15, 13)":
                    fail(f"the toggle did not switch to light ({theme}, {ground})")
                # Every text a person reads against the page must stay legible.
                weak = page.evaluate("""() => {
                    const lum = c => { const [r,g,b] = c.match(/\\d+(\\.\\d+)?/g).slice(0,3).map(Number)
                        .map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
                        return .2126*r + .7152*g + .0722*b; };
                    const bg = lum(getComputedStyle(document.body).backgroundColor);
                    const out = [];
                    for (const sel of ['.hdr h1', '.stat span', '.group-label', '.bk-name',
                                       '.bk-meta', '.wait .n', '.wait .s', '.meta', '.name']) {
                        const e = document.querySelector(sel);
                        if (!e) continue;
                        const f = lum(getComputedStyle(e).color);
                        const ratio = (Math.max(f, bg) + .05) / (Math.min(f, bg) + .05);
                        if (ratio < 4.5) out.push(sel + ' ' + ratio.toFixed(1));
                    }
                    return out;
                }""")
                for w in weak:
                    fail(f"light mode text under 4.5:1 contrast: {w}")
                overflow = page.evaluate("document.documentElement.scrollWidth"
                                         " - document.documentElement.clientWidth")
                if overflow > 0:
                    fail(f"light mode scrolls sideways by {overflow}px")
                no_junk("body")
                page.screenshot(path=f"/tmp/ui-{label}-light.png", full_page=True)
                page.reload()
                page.wait_for_timeout(1200)
                if page.evaluate("document.documentElement.dataset.theme") != "light":
                    fail("the light choice was forgotten on reload")
                page.locator(".dock .theme" if phone else ".hdr-actions .theme").click()
                page.wait_for_timeout(150)
                if page.evaluate("document.documentElement.dataset.theme"):
                    fail("the toggle did not switch back to dark")

            checked += 1
            print(f"  {label:10} {width:>5}px  ok" if not failures
                  else f"  {label:10} {width:>5}px  checked")
            page.close(); ctx.close()

        # ---- dismissing one product leaves the rest of its sweep. Once, at
        # the end, because it changes the data every other width reads.
        ctx = browser.new_context(viewport={"width": 393, "height": 900})
        ctx.route("https://img.test/**", lambda route: route.fulfill(
            status=200, content_type="image/png", body=image))
        page = ctx.new_page()
        page.goto("http://127.0.0.1:8094/")
        page.wait_for_timeout(700)
        if page.locator("#pw").is_visible():
            page.fill("#pw", "pw")
            page.click("text=SIGN IN")
            page.wait_for_timeout(1500)
        page.locator(".bucket", has_text="comoli").click()
        page.wait_for_timeout(250)
        before = page.locator(".bucket-sheet .item").count()
        gone = page.get_attribute(".bucket-sheet .item >> nth=0 >> .x", "data-h")
        page.click(".bucket-sheet .item >> nth=0 >> .x")
        page.wait_for_timeout(600)
        after = page.locator(".bucket-sheet .item").count()
        if after != before - 1:
            failures.append(f"dismissing one product took {before - after} with it")
        events = page.evaluate("fetch('/api/events').then(r => r.json())")["events"]
        left = sorted(h for e in events for h in (e["payload"].get("handles") or [])
                      if h[:1] == "c" and h[1:].isdigit())
        if gone in left or len(left) != 11:
            failures.append(f"server-side, dismissing {gone} left {left}")
        if "11 buyable items" not in page.inner_text(".sheet-head"):
            failures.append("the sheet's count did not follow the dismissal")
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
    print("\nAll layout and interaction checks passed. Screenshots in /tmp/ui-*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
