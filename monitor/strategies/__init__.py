"""Strategy registry and platform auto-detection.

Adding a watch should be paste-a-URL simple, so `detect` sniffs the platform
rather than making you pick one. Retailer-specific strategies match on hostname
first; Shopify is the generic fallback because it's identified by probing
endpoints rather than by domain.
"""
from ..statemachine import CheckResult
from . import announce, bestbuy, shopify, target

REGISTRY = {
    shopify.NAME: shopify,
    target.NAME: target,
    bestbuy.NAME: bestbuy,
    announce.NAME: announce,
}

# Hostname-matched strategies are cheap to rule out, so try them first. announce
# precedes shopify because a store's /search URL would otherwise be claimed as a
# collection watch by shopify's origin probe.
DETECT_ORDER = (target, bestbuy, announce, shopify)


async def check(watch: dict) -> CheckResult:
    strategy = REGISTRY.get(watch.get("strategy"))
    if strategy is None:
        return CheckResult(ok=False,
                           error=f"unknown strategy {watch.get('strategy')!r}")
    return await strategy.check(watch)


async def detect(url: str) -> dict | None:
    for strategy in DETECT_ORDER:
        try:
            found = await strategy.detect(url)
        except Exception as exc:  # a broken sniffer must not block the others
            continue
        if found:
            return found
    return None
