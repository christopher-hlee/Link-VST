"""The rules that must not regress."""
import pytest

from monitor.statemachine import (
    IN_STOCK, NEW_PRODUCT, OUT_OF_STOCK, PRICE_DROP, RESTOCK, UNKNOWN,
    WATCH_FAILING, WATCH_RECOVERED, CheckResult, decide, next_interval,
)


def kinds(decision):
    return [e.kind for e in decision.events]


def _decide(**kw):
    base = dict(kind="product", prev_state=UNKNOWN, prev_failures=0,
                prev_baseline=None, prev_price=None)
    base.update(kw)
    return decide(**base)


# --- the central invariant -------------------------------------------------

@pytest.mark.parametrize("prev", [IN_STOCK, OUT_OF_STOCK, UNKNOWN])
@pytest.mark.parametrize("error,status", [
    ("timeout", None), ("HTTP 403", 403), ("HTTP 500", 500),
    ("parse error", 200),
])
def test_failed_check_never_changes_state(prev, error, status):
    """A failure means 'unknown', never 'sold out'."""
    d = _decide(prev_state=prev,
                result=CheckResult(ok=False, error=error, http_status=status))
    assert d.state == prev
    assert d.failures == 1
    assert RESTOCK not in kinds(d)


def test_failure_does_not_clobber_price_or_baseline():
    d = decide(kind="collection", prev_state=IN_STOCK, prev_failures=0,
               prev_baseline=["a", "b"], prev_price=42.0,
               result=CheckResult(ok=False, error="boom"))
    assert d.baseline == ["a", "b"]
    assert d.price == 42.0


# --- restock transitions ---------------------------------------------------

def test_restock_fires_on_out_to_in():
    d = _decide(prev_state=OUT_OF_STOCK,
                result=CheckResult(ok=True, state=IN_STOCK, price=120.0,
                                   cart_url="https://s.com/cart/1:1"))
    assert kinds(d) == [RESTOCK]
    assert d.state == IN_STOCK
    assert d.events[0].payload["cart_url"] == "https://s.com/cart/1:1"


def test_first_check_of_in_stock_item_is_silent():
    """Adding a watch for something already in stock must not ping you."""
    d = _decide(prev_state=UNKNOWN, result=CheckResult(ok=True, state=IN_STOCK))
    assert kinds(d) == []
    assert d.state == IN_STOCK


def test_still_in_stock_does_not_realert():
    d = _decide(prev_state=IN_STOCK, result=CheckResult(ok=True, state=IN_STOCK))
    assert kinds(d) == []


def test_going_out_of_stock_is_silent():
    d = _decide(prev_state=IN_STOCK, result=CheckResult(ok=True, state=OUT_OF_STOCK))
    assert kinds(d) == []
    assert d.state == OUT_OF_STOCK


def test_restock_after_failures_still_fires():
    """A watch that recovers straight into stock reports both facts."""
    d = _decide(prev_state=OUT_OF_STOCK, prev_failures=7,
                result=CheckResult(ok=True, state=IN_STOCK))
    assert kinds(d) == [WATCH_RECOVERED, RESTOCK]
    assert d.failures == 0


# --- failure alerting ------------------------------------------------------

def test_failing_alert_fires_once_at_threshold():
    fired = []
    failures = 0
    for _ in range(12):
        d = _decide(prev_state=IN_STOCK, prev_failures=failures,
                    result=CheckResult(ok=False, error="HTTP 403"))
        failures = d.failures
        fired.extend(k for k in kinds(d) if k == WATCH_FAILING)
    assert fired == [WATCH_FAILING], "should alert on the crossing only"
    assert failures == 12


def test_recovery_only_after_real_failure_run():
    d = _decide(prev_state=IN_STOCK, prev_failures=2,
                result=CheckResult(ok=True, state=IN_STOCK))
    assert WATCH_RECOVERED not in kinds(d)


# --- 304 handling ----------------------------------------------------------

def test_not_modified_preserves_everything():
    d = decide(kind="collection", prev_state=OUT_OF_STOCK, prev_failures=3,
               prev_baseline=["x"], prev_price=9.99,
               result=CheckResult(ok=True, not_modified=True))
    assert (d.state, d.baseline, d.price, d.failures) == (
        OUT_OF_STOCK, ["x"], 9.99, 0)
    assert kinds(d) == []


# --- collections -----------------------------------------------------------

def test_collection_first_check_sets_baseline_silently():
    d = decide(kind="collection", prev_state=UNKNOWN, prev_failures=0,
               prev_baseline=None, prev_price=None,
               result=CheckResult(ok=True, handles=["a", "b"]))
    assert kinds(d) == []
    assert d.baseline == ["a", "b"]


def test_collection_new_handle_fires():
    d = decide(kind="collection", prev_state=IN_STOCK, prev_failures=0,
               prev_baseline=["a"], prev_price=None,
               result=CheckResult(ok=True, handles=["a", "b", "c"]))
    assert kinds(d) == [NEW_PRODUCT]
    assert d.events[0].payload["handles"] == ["b", "c"]
    assert d.baseline == ["a", "b", "c"]


def test_handle_disappearing_and_returning_does_not_realert():
    d = decide(kind="collection", prev_state=IN_STOCK, prev_failures=0,
               prev_baseline=["a", "b"], prev_price=None,
               result=CheckResult(ok=True, handles=["a"]))
    assert kinds(d) == []
    d2 = decide(kind="collection", prev_state=IN_STOCK, prev_failures=0,
                prev_baseline=d.baseline, prev_price=None,
                result=CheckResult(ok=True, handles=["a", "b"]))
    assert kinds(d2) == []


# --- price -----------------------------------------------------------------

def test_price_drop_detected():
    d = _decide(prev_state=IN_STOCK, prev_price=200.0,
                result=CheckResult(ok=True, state=IN_STOCK, price=150.0))
    assert PRICE_DROP in kinds(d)
    assert d.price == 150.0


def test_price_increase_is_silent():
    d = _decide(prev_state=IN_STOCK, prev_price=100.0,
                result=CheckResult(ok=True, state=IN_STOCK, price=180.0))
    assert kinds(d) == []


# --- intervals -------------------------------------------------------------

def test_hot_interval_only_while_armed():
    assert next_interval(300, 45, hot_until_ts=200.0, now_ts=100.0) == 45
    assert next_interval(300, 45, hot_until_ts=50.0, now_ts=100.0) == 300
    assert next_interval(300, 45, hot_until_ts=None, now_ts=100.0) == 300
