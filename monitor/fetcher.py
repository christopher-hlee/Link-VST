"""Shared async HTTP layer: one client, per-domain politeness, conditional GETs.

Getting banned is the real failure mode for this app, so the throttling here
matters more than raw speed. Every outbound request goes through `fetch`.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from .config import (
    PER_DOMAIN_CONCURRENCY, PER_DOMAIN_MIN_GAP, PROXY_HOSTS, PROXY_KEY,
    PROXY_URL, REQUEST_TIMEOUT, USER_AGENT,
)

log = logging.getLogger("monitor.fetcher")

_client: httpx.AsyncClient | None = None
_limiters: dict[str, "DomainLimiter"] = {}

# Accept-Encoding is deliberately absent: httpx sets it from the codecs it can
# actually decode. Hardcoding "gzip, deflate, br" here claimed brotli support
# the client did not have — Shopify honoured it, httpx received bytes it could
# not read, and .json() failed. The request looked successful, so the failure
# surfaced as "no supported platform detected" rather than as a decode error.
# Never advertise a capability you do not have.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


class DomainLimiter:
    """Bounded concurrency plus a minimum gap between requests to one host."""

    def __init__(self, concurrency: int, min_gap: float):
        self._sem = asyncio.Semaphore(concurrency)
        self._min_gap = min_gap
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._sem.acquire()
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_gap:
                await asyncio.sleep(self._min_gap - gap)
            self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False


def limiter_for(url: str) -> DomainLimiter:
    host = urlparse(url).netloc.lower()
    if host not in _limiters:
        _limiters[host] = DomainLimiter(PER_DOMAIN_CONCURRENCY, PER_DOMAIN_MIN_GAP)
    return _limiters[host]


def route(url: str) -> tuple[str, dict[str, str]]:
    """Rewrite to the egress proxy when this host is configured for it.

    Returns the URL to actually request plus any extra headers. Hosts not in
    PROXY_HOSTS are returned untouched, so the proxy stays opt-in per host
    rather than becoming a single point of failure for every request.
    """
    if not (PROXY_URL and PROXY_KEY):
        return url, {}
    host = urlparse(url).netloc.lower()
    if host not in PROXY_HOSTS:
        return url, {}
    return f"{PROXY_URL}?url={quote(url, safe='')}", {"X-Proxy-Key": PROXY_KEY}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class FetchResponse:
    ok: bool
    status: int | None = None
    text: str = ""
    json: object = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    latency_ms: int = 0


async def fetch(url: str, *, etag: str | None = None,
                last_modified: str | None = None,
                headers: dict[str, str] | None = None,
                retries: int = 2) -> FetchResponse:
    """GET with conditional headers, per-domain throttling, and backoff.

    A 304 comes back as ok=True with not_modified=True — the caller should treat
    that as "nothing changed", not as a failure.
    """
    req_headers = dict(headers or {})
    if etag:
        req_headers["If-None-Match"] = etag
    if last_modified:
        req_headers["If-Modified-Since"] = last_modified

    request_url, proxy_headers = route(url)
    req_headers.update(proxy_headers)

    client = get_client()
    # Deliberately keyed on the ORIGINAL host: the rate limit protects the
    # retailer we are polling, not the proxy in front of it. Keying on the
    # proxy would collapse every proxied host into one bucket and quietly
    # discard the politeness that keeps us unbanned.
    limiter = limiter_for(url)
    started = time.monotonic()
    last_error = "unknown error"
    last_status: int | None = None

    for attempt in range(retries + 1):
        try:
            async with limiter:
                resp = await client.get(request_url, headers=req_headers)
            latency = int((time.monotonic() - started) * 1000)
            last_status = resp.status_code

            if resp.status_code == 304:
                return FetchResponse(ok=True, status=304, not_modified=True,
                                     etag=etag, last_modified=last_modified,
                                     latency_ms=latency)

            if resp.status_code == 200:
                parsed = None
                try:
                    parsed = resp.json()
                except (ValueError, TypeError, UnicodeDecodeError):
                    # Not JSON, or undecodable. Callers that need JSON check for
                    # None; .text still carries whatever arrived for diagnosis.
                    pass
                return FetchResponse(
                    ok=True, status=200, text=resp.text, json=parsed,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    latency_ms=latency,
                )

            # Rate limited or server-side: back off and retry.
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
                if attempt < retries:
                    await asyncio.sleep(_backoff(attempt, resp.headers.get("Retry-After")))
                    continue
                return FetchResponse(ok=False, status=resp.status_code,
                                     error=last_error, latency_ms=latency)

            # 403/404 and friends are answers, not transient faults. Don't retry.
            return FetchResponse(ok=False, status=resp.status_code,
                                 error=f"HTTP {resp.status_code}",
                                 latency_ms=latency)

        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries:
            await asyncio.sleep(_backoff(attempt, None))

    return FetchResponse(ok=False, status=last_status, error=last_error,
                         latency_ms=int((time.monotonic() - started) * 1000))


def _backoff(attempt: int, retry_after: str | None) -> float:
    """Honor Retry-After when the server sends it; otherwise jittered exponential."""
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except (TypeError, ValueError):
            pass
    return min(2.0 ** attempt + random.uniform(0, 0.5), 30.0)
