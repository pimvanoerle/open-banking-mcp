"""get_balances should span every connected bank when none is named."""

from datetime import datetime, timezone

import pytest

from open_banking_mcp import server
from open_banking_mcp.service import Result

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeService:
    HOLDERS = {
        "bank-a": ([{"account_id": "a1", "display_name": "A Current"}], []),
        "bank-b": (
            [{"account_id": "b1", "display_name": "B Current"}],
            [{"account_id": "b-card", "display_name": "B Card"}],
        ),
    }
    BALANCES = {"a1": 100.0, "b1": 50.0, "b-card": 300.0}

    async def accounts(self, provider, fresh=False):
        return Result(self.HOLDERS[provider][0], NOW, "cache")

    async def cards(self, provider, fresh=False):
        return Result(self.HOLDERS[provider][1], NOW, "cache")

    async def balance(self, provider, account_id, fresh=False):
        return Result(
            {"currency": "GBP", "current": self.BALANCES[account_id], "available": 1.0},
            NOW,
            "cache",
        )

    card_balance = balance


@pytest.fixture
def two_banks(monkeypatch):
    monkeypatch.setattr(server, "service", lambda: FakeService())
    monkeypatch.setattr(server, "_providers", lambda: ["bank-a", "bank-b"])


def _payload(result: dict) -> dict:
    return result.get("data", result)


async def test_no_provider_spans_all_banks(two_banks):
    result = _payload(await server.get_balances())
    rows = result["balances"]
    assert {(r["provider_id"], r["id"]) for r in rows} == {
        ("bank-a", "a1"),
        ("bank-b", "b1"),
        ("bank-b", "b-card"),
    }
    # Totals cover current accounts only, not the card balance.
    assert result["account_totals"] == {"GBP": 150.0}


async def test_named_provider_still_scopes_to_one_bank(two_banks):
    result = _payload(await server.get_balances(provider_id="bank-b"))
    assert {r["provider_id"] for r in result["balances"]} == {"bank-b"}
    assert result["account_totals"] == {"GBP": 50.0}


async def test_no_banks_connected_raises(monkeypatch):
    monkeypatch.setattr(server, "service", lambda: FakeService())
    monkeypatch.setattr(server, "_providers", lambda: [])
    with pytest.raises(ValueError, match="No banks are connected"):
        await server.get_balances()
