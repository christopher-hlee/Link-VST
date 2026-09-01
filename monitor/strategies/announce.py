"""Announcement watcher — detects a product coming into existence.

Every other strategy answers "is this in stock?", which presupposes a product
page to poll. That is useless for the case it was written for: a leaked console
edition with no SKU, no listing, and no URL yet. By the time there is something
to watch, preorders have opened and closed.

So this one watches for *arrival* rather than availability — new entries in a
feed, or new hits for a search term — and filters them by keyword.

It reports its findings as `handles`, which means the existing collection
state machine diffs them against a baseline with no changes at all: first check
records what is already there, and only genuinely new entries alert.
"""
import hashlib
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..fetcher import fetch
from ..statemachine import CheckResult, IN_STOCK, OUT_OF_STOCK

NAME = "announce"

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_FEEDISH = re.compile(r"\.(atom|rss|xml)(\?|$)|/(feed|rss|atom)/?(\?|$)", re.I)


def parse_keywords(raw: str | None) -> tuple[str, list[str]]:
    """Parse a keyword expression into (mode, terms).

    Two forms, because "any of these" and "all of these" are both useful and
    only one of them was supported:

        zelda, ocarina      ANY — fires on either. Good for synonyms.
        zelda + ocarina     ALL — fires only when both appear. Good for
                            narrowing a broad term on a busy feed.

    Terms are matched as phrases, so "switch 2" matches that exact string.
    Empty means match everything.
    """
    if not raw or not raw.strip():
        return "any", []
    if "+" in raw:
        return "all", [k.strip().lower() for k in raw.split("+") if k.strip()]
    return "any", [k.strip().lower() for k in raw.split(",") if k.strip()]


def matches(text: str, mode: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = text.lower()
    hits = (k in haystack for k in keywords)
    return all(hits) if mode == "all" else any(hits)


def entry_id(candidate: str | None, title: str) -> str:
    """Stable id for an entry, so a reworded title doesn't re-alert."""
    if candidate:
        return candidate.strip()
    return "h:" + hashlib.sha1(title.strip().lower().encode()).hexdigest()[:16]


# ------------------------------------------------------------------ parsing

def parse_feed(text: str) -> list[dict]:
    """Parse Atom or RSS 2.0 into {id, title, link, summary}.

    Raises ValueError if the document is not a feed. `<html>...</html>` is
    perfectly well-formed XML, so parsing alone proves nothing — and treating an
    error page as an empty feed would record a baseline of nothing, making the
    next real response look like a flood of brand-new entries.
    """
    root = ET.fromstring(text)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag not in ("feed", "rss"):
        raise ValueError(f"root element <{tag}> is not a feed")
    entries: list[dict] = []

    # Atom
    for el in root.findall("a:entry", ATOM_NS):
        title = (el.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (el.findtext("a:summary", default="", namespaces=ATOM_NS)
                   or el.findtext("a:content", default="", namespaces=ATOM_NS) or "")
        link_el = el.find("a:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else None
        raw_id = el.findtext("a:id", default="", namespaces=ATOM_NS)
        entries.append({"id": entry_id(raw_id or link, title), "title": title,
                        "link": link, "summary": summary})

    # RSS 2.0 (no namespace)
    for el in root.iter("item"):
        title = (el.findtext("title") or "").strip()
        link = el.findtext("link")
        raw_id = el.findtext("guid") or link
        entries.append({"id": entry_id(raw_id, title), "title": title,
                        "link": link, "summary": el.findtext("description") or ""})

    return entries


def parse_shopify_search(payload: object) -> list[dict]:
    """Shopify's /search/suggest.json product results."""
    if not isinstance(payload, dict):
        return []
    products = (payload.get("resources", {}).get("results", {}).get("products")
                if isinstance(payload.get("resources"), dict) else None)
    if products is None:
        products = payload.get("products") or []
    out = []
    for p in products:
        if not isinstance(p, dict):
            continue
        handle = p.get("handle") or p.get("url") or p.get("title")
        title = p.get("title") or ""
        out.append({"id": entry_id(handle, title), "title": title,
                    "link": p.get("url"), "summary": p.get("body") or ""})
    return out


# ----------------------------------------------------------------- checking

async def check(watch: dict) -> CheckResult:
    mode, keywords = parse_keywords(watch.get("target_ref"))
    resp = await fetch(watch["url"], etag=watch.get("etag"),
                       last_modified=watch.get("last_modified"))

    if resp.not_modified:
        return CheckResult(ok=True, not_modified=True, etag=resp.etag,
                           last_modified=resp.last_modified, http_status=304)
    if not resp.ok:
        return CheckResult(ok=False, http_status=resp.status, error=resp.error,
                           rate_limited=resp.rate_limited,
                           retry_after=resp.retry_after)

    try:
        if resp.json is not None:
            entries = parse_shopify_search(resp.json)
            source = "search-json"
        else:
            entries = parse_feed(resp.text)
            source = "feed"
    except (ET.ParseError, ValueError) as exc:
        return CheckResult(ok=False, http_status=resp.status,
                           error=f"could not parse as feed or JSON: {exc}")

    hits = [e for e in entries
            if matches(f"{e['title']} {e['summary']}", mode, keywords)]

    return CheckResult(
        ok=True,
        state=IN_STOCK if hits else OUT_OF_STOCK,
        handles=[e["id"] for e in hits],
        title=watch.get("name"),
        product_url=watch["url"],
        http_status=resp.status,
        etag=resp.etag,
        last_modified=resp.last_modified,
        extra={
            "source": source,
            "entries_seen": len(entries),
            "matches": len(hits),
            "keywords": keywords,
            "keyword_mode": mode,
            # An announcement is news, not a catalogue: the alert copy and the
            # link target both differ from a storefront drop.
            "is_announcement": True,
            # Titles are what the alert actually shows, keyed by entry id so the
            # collection diff can name the new arrivals.
            "titles": {e["id"]: e["title"] for e in hits},
            "links": {e["id"]: e["link"] for e in hits if e["link"]},
        },
    )


# ---------------------------------------------------------------- detection

async def detect(url: str) -> dict | None:
    """Feeds and search endpoints only — never a plain product page."""
    path = urlparse(url).path
    if not _FEEDISH.search(url) and "/search" not in path:
        return None
    # /collections/*.atom is feed-shaped, but the shopify strategy pulls real
    # product handles out of it, so defer to that one.
    if "/collections/" in path:
        return None

    resp = await fetch(url)
    if not resp.ok:
        return None

    kind_hint = None
    if resp.json is not None and parse_shopify_search(resp.json):
        kind_hint = "search-json"
    else:
        try:
            parse_feed(resp.text)
            kind_hint = "feed"
        except (ET.ParseError, ValueError):
            kind_hint = None
    if kind_hint is None:
        return None

    host = urlparse(url).netloc.replace("www.", "")
    return {
        "strategy": NAME,
        # Reuses the collection diff: baseline first, then only new arrivals.
        "kind": "collection",
        "name": f"{host} · announcements",
        "brand": host,
        "url": url,
        "target_ref": "",   # keywords; empty matches every new entry
        "detected_via": kind_hint,
        "note": "Set target_ref to comma-separated keywords, e.g. "
                "'zelda, switch 2, ocarina'. Empty alerts on every new entry.",
    }
