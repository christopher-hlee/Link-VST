import pytest

from monitor import fetcher


@pytest.fixture(autouse=True)
def _reset_http(monkeypatch):
    """Fresh client per test, no inter-request delay, no retry backoff.

    Production keeps both — they are what stops the app getting IP-banned — but
    waiting them out would make the suite take half a minute.
    """
    fetcher._client = None
    fetcher._limiters.clear()
    monkeypatch.setattr(fetcher, "PER_DOMAIN_MIN_GAP", 0.0)
    monkeypatch.setattr(fetcher, "_backoff", lambda attempt, retry_after: 0.0)
    yield
    fetcher._client = None
    fetcher._limiters.clear()
