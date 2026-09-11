from datetime import datetime, timedelta, timezone

import pytest

from open_banking_mcp.cache import Cache
from open_banking_mcp.service import DataService, Result


class FakeClient:
    """Counts calls so we can prove the cache actually prevents network use."""

    def __init__(self):
        self.calls = []
        self.accounts_payload = [{"account_id": "a1", "display_name": "CURRENT"}]
        self.cards_payload = [{"account_id": "c1"}]
        self.balance_payload = {"current": 100.0, "currency": "GBP"}

    async def accounts(self, p):
        self.calls.append("accounts"); return self.accounts_payload

    async def cards(self, p):
        self.calls.append("cards"); return self.cards_payload

    async def info(self, p):
        self.calls.append("info"); return [{"full_name": "John Doe"}]

    async def account_balance(self, p, a):
        self.calls.append("balance"); return self.balance_payload

    async def card_balance(self, p, c):
        self.calls.append("card_balance"); return {"current": 5.0}

    async def account_transactions(self, p, a, from_date=None, to_date=None):
        self.calls.append("transactions")
        return [{"transaction_id": "t1", "timestamp": "2026-09-01T00:00:00Z",
                 "amount": -9.0, "description": "LIVE TX"}]

    async def pending_transactions(self, p, a):
        self.calls.append("pending")
        return [{"transaction_id": "p1", "timestamp": "2026-09-02T00:00:00Z",
                 "amount": -1.0, "description": "PENDING TX"}]

    async def card_transactions(self, p, c, from_date=None, to_date=None):
        self.calls.append("card_transactions"); return []

    async def pending_card_transactions(self, p, c):
        self.calls.append("pending_card"); return []

    async def standing_orders(self, p, a):
        self.calls.append("standing_orders"); return []

    async def direct_debits(self, p, a):
        self.calls.append("direct_debits"); return []


@pytest.fixture
def svc(tmp_path):
    client = FakeClient()
    return DataService(client, Cache(tmp_path / "c.db")), client


async def test_first_read_fetches_then_serves_from_cache(svc):
    service, client = svc
    r1 = await service.accounts("mock")
    assert r1.source == "live"

    r2 = await service.accounts("mock")
    assert r2.source == "cache"
    assert client.calls.count("accounts") == 1, "second read must not hit the API"


async def test_empty_cache_does_not_masquerade_as_no_accounts(svc):
    """A cold cache must fetch, not report zero accounts."""
    service, client = svc
    r = await service.accounts("mock")
    assert r.data and "accounts" in client.calls


async def test_fresh_bypasses_cache_and_updates_it(svc):
    service, client = svc
    await service.balance("mock", "a1")
    client.balance_payload = {"current": 999.0, "currency": "GBP"}

    cached = await service.balance("mock", "a1")
    assert cached.data["current"] == 100.0, "cache should still hold the old value"

    fresh = await service.balance("mock", "a1", fresh=True)
    assert fresh.data["current"] == 999.0 and fresh.source == "live"

    after = await service.balance("mock", "a1")
    assert after.data["current"] == 999.0, "fresh read must write through to cache"


async def test_stale_flag_tracks_max_age(tmp_path):
    client = FakeClient()
    cache = Cache(tmp_path / "c.db")
    service = DataService(client, cache, max_age=timedelta(hours=25))
    await service.accounts("mock")

    # Backdate the snapshot to two days ago.
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with cache._conn() as conn:
        conn.execute("UPDATE snapshots SET synced_at = ?", (old,))

    r = await service.accounts("mock")
    assert r.stale and r.describe_age() == "2 days old"


async def test_live_results_are_never_stale(svc):
    service, _ = svc
    r = await service.accounts("mock", fresh=True)
    assert r.source == "live" and not r.stale and r.describe_age() == "live"


async def test_fresh_transactions_pull_settled_and_pending(svc):
    service, client = svc
    r = await service.transactions("mock", "a1", fresh=True)
    descriptions = {t["description"] for t in r.data}
    assert descriptions == {"LIVE TX", "PENDING TX"}
    assert "transactions" in client.calls and "pending" in client.calls


async def test_fresh_transactions_require_a_specific_account(svc):
    service, _ = svc
    with pytest.raises(ValueError, match="per-account"):
        await service.transactions("mock", fresh=True)


async def test_card_ids_route_to_card_endpoints(svc):
    service, client = svc
    await service.transactions("mock", "c1", fresh=True)
    assert "card_transactions" in client.calls
    assert "transactions" not in client.calls


async def test_sync_pulls_everything_and_reports(svc):
    service, client = svc
    report = await service.sync("mock", history_days=30)
    assert report.ok
    assert report.accounts == 1 and report.cards == 1
    assert report.transactions == 1 and report.pending == 1

    run = service._cache.last_run("mock")
    assert run["status"] == "ok"


async def test_sync_survives_a_failing_endpoint(svc):
    service, client = svc

    async def boom(p, a):
        raise __import__("open_banking_mcp.client", fromlist=["TrueLayerError"]).TrueLayerError(
            403, "/standing_orders", "not supported by provider")

    client.standing_orders = boom
    report = await service.sync("mock", history_days=30)

    assert not report.ok
    assert any("standing_orders" in e for e in report.errors)
    # The rest of the sync still landed.
    assert report.transactions == 1
    assert service._cache.last_run("mock")["status"] == "error"


def test_result_age_wording():
    now = datetime.now(timezone.utc)
    def r(delta): return Result([], now - delta, "cache")
    assert r(timedelta(seconds=30)).describe_age() == "just synced"
    assert r(timedelta(minutes=30)).describe_age() == "30 minutes old"
    assert r(timedelta(hours=5)).describe_age() == "5 hours old"
    assert r(timedelta(days=3)).describe_age() == "3 days old"
    assert Result([], None, "cache").describe_age() == "never synced"
    assert Result([], None, "cache").stale
