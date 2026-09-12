"""Guards for things that only matter once real bank data is involved."""

import stat

import httpx
import pytest
import respx

from open_banking_mcp.cache import Cache
from open_banking_mcp.client import RateLimited, TrueLayerClient, TrueLayerError
from open_banking_mcp.service import DataService


def test_cache_is_not_world_readable(tmp_path):
    """A year of real transactions must not sit at 0644."""
    path = tmp_path / "nested" / "cache.db"
    cache = Cache(path)
    cache.put_snapshot("p", "accounts", [{"account_id": "a1"}])

    mode = stat.S_IMODE(path.stat().st_mode)
    assert not mode & stat.S_IROTH, f"cache.db is world-readable ({oct(mode)})"
    assert not mode & stat.S_IRGRP, f"cache.db is group-readable ({oct(mode)})"
    assert mode == 0o600

    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert dir_mode == 0o700, f"cache dir is {oct(dir_mode)}"


class _Tokens:
    async def access_token(self, provider_id):
        return "at"


@respx.mock
async def test_429_raises_rate_limited_with_retry_after(settings):
    respx.get(f"{settings.api_base_url}/accounts").mock(
        return_value=httpx.Response(429, headers={"retry-after": "120"}, text="slow down")
    )
    client = TrueLayerClient(settings, _Tokens())

    with pytest.raises(RateLimited) as exc:
        await client.accounts("mock")
    assert exc.value.retry_after == 120
    assert "TRUELAYER_PSU_IP" in str(exc.value)
    assert isinstance(exc.value, TrueLayerError)


@respx.mock
async def test_psu_ip_header_sent_only_when_configured(settings):
    route = respx.get(f"{settings.api_base_url}/accounts").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await TrueLayerClient(settings, _Tokens()).accounts("mock")
    assert "x-psu-ip" not in route.calls.last.request.headers

    await TrueLayerClient(settings, _Tokens(), psu_ip="1.2.3.4").accounts("mock")
    assert route.calls.last.request.headers["x-psu-ip"] == "1.2.3.4"


class _RateLimitedClient:
    """Succeeds on discovery, then throttles on the first per-account call."""

    def __init__(self):
        self.calls = 0
        self.accounts_payload = [{"account_id": f"a{i}"} for i in range(5)]

    async def info(self, p): return [{}]
    async def accounts(self, p): return self.accounts_payload
    async def cards(self, p): return []

    async def account_balance(self, p, a):
        self.calls += 1
        raise RateLimited("/balance", "too many", 60.0)

    async def account_transactions(self, p, a, from_date=None, to_date=None): return []
    async def pending_transactions(self, p, a): return []
    async def standing_orders(self, p, a): return []
    async def direct_debits(self, p, a): return []


async def test_sync_stops_at_first_rate_limit(tmp_path):
    """Hammering the remaining accounts would burn the allowance and still fail."""
    client = _RateLimitedClient()
    service = DataService(client, Cache(tmp_path / "c.db"))

    report = await service.sync("mock", history_days=30)

    assert report.rate_limited
    assert not report.ok
    assert client.calls == 1, f"kept going after a 429 ({client.calls} calls)"
    assert any("Rate limited" in e for e in report.errors)
    # Whatever landed before the limit is still cached.
    assert service._cache.get_snapshot("mock", "accounts") is not None
