"""MCP server exposing the bank data as tools.

Reads come from the local cache by default so the server stays inside
TrueLayer's unattended rate limits. Every tool takes `fresh` to force a live
call, and every response carries how old the data is.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from mcp.server import MCPServer

from .auth import TokenManager
from .cache import Cache
from .client import TrueLayerClient, TrueLayerError
from .config import ConfigError, load_settings
from .service import DataService
from .storage import build_store

mcp = MCPServer("open-banking")

_service: DataService | None = None
_settings = None


def service() -> DataService:
    global _service, _settings
    if _service is None:
        _settings = load_settings()
        store = build_store(_settings)
        client = TrueLayerClient(
            _settings, TokenManager(_settings, store), psu_ip=_settings.psu_ip
        )
        _service = DataService(
            client,
            Cache(_settings.cache_file),
            max_age=timedelta(hours=_settings.max_age_hours),
        )
    return _service


def _providers() -> list[str]:
    settings = _settings or load_settings()
    return build_store(settings).providers()


def _one_provider(provider_id: str | None) -> str:
    """Default to the only connected bank when the caller didn't name one."""
    if provider_id:
        return provider_id
    connected = _providers()
    if not connected:
        raise ValueError(
            "No banks are connected. Run 'open-banking-mcp auth' in a terminal."
        )
    if len(connected) > 1:
        raise ValueError(
            f"Several banks are connected ({', '.join(connected)}); "
            f"pass provider_id to say which one."
        )
    return connected[0]


def _wrap(result) -> dict:
    payload = result.envelope()
    if payload["stale"]:
        payload["warning"] = (
            f"This data is {payload['age']} and may be out of date. "
            f"Pass fresh=true to pull live figures."
        )
    return payload


@mcp.tool()
async def list_banks() -> dict:
    """List connected banks and when each was last synced."""
    out = []
    for provider_id in _providers():
        run = service()._cache.last_run(provider_id)
        out.append(
            {
                "provider_id": provider_id,
                "last_sync": run["finished_at"] if run else None,
                "last_sync_status": run["status"] if run else "never",
            }
        )
    return {"banks": out}


@mcp.tool()
async def list_accounts(provider_id: str | None = None, fresh: bool = False) -> dict:
    """List bank accounts, with sort code and account number where available.

    Set fresh=true to bypass the local cache and query the bank directly.
    """
    return _wrap(await service().accounts(_one_provider(provider_id), fresh=fresh))


@mcp.tool()
async def list_cards(provider_id: str | None = None, fresh: bool = False) -> dict:
    """List credit and debit cards."""
    return _wrap(await service().cards(_one_provider(provider_id), fresh=fresh))


@mcp.tool()
async def get_balance(
    account_id: str, provider_id: str | None = None, fresh: bool = False
) -> dict:
    """Get the current and available balance for one account.

    Use fresh=true when the answer must reflect a payment made moments ago.
    """
    return _wrap(
        await service().balance(_one_provider(provider_id), account_id, fresh=fresh)
    )


@mcp.tool()
async def get_card_balance(
    card_id: str, provider_id: str | None = None, fresh: bool = False
) -> dict:
    """Get a card's balance, credit limit and payment due details."""
    return _wrap(
        await service().card_balance(_one_provider(provider_id), card_id, fresh=fresh)
    )


@mcp.tool()
async def get_balances(provider_id: str | None = None, fresh: bool = False) -> dict:
    """Get balances for every account and card at once -- the 'how am I doing' view."""
    svc, provider = service(), _one_provider(provider_id)
    accounts = (await svc.accounts(provider, fresh=fresh)).data or []
    cards = (await svc.cards(provider, fresh=fresh)).data or []

    rows, oldest = [], None
    for holder, kind in [(a, "account") for a in accounts] + [(c, "card") for c in cards]:
        hid = holder.get("account_id")
        result = await (
            svc.card_balance(provider, hid, fresh=fresh)
            if kind == "card"
            else svc.balance(provider, hid, fresh=fresh)
        )
        balance = result.data or {}
        rows.append(
            {
                "kind": kind,
                "id": hid,
                "name": holder.get("display_name") or holder.get("card_network"),
                "currency": balance.get("currency"),
                "current": balance.get("current"),
                "available": balance.get("available"),
                "credit_limit": balance.get("credit_limit"),
            }
        )
        if result.as_of and (oldest is None or result.as_of < oldest):
            oldest = result.as_of

    totals: dict[str, float] = {}
    for row in rows:
        if row["kind"] == "account" and row["currency"] and row["current"] is not None:
            totals[row["currency"]] = totals.get(row["currency"], 0.0) + row["current"]

    from .service import Result

    return _wrap(
        Result(
            {"balances": rows, "account_totals": totals},
            oldest,
            "live" if fresh else "cache",
            timedelta(hours=(_settings.max_age_hours if _settings else 25)),
        )
    )


@mcp.tool()
async def get_transactions(
    account_id: str | None = None,
    provider_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    include_pending: bool = True,
    limit: int = 100,
    fresh: bool = False,
) -> dict:
    """Query transactions across accounts and cards.

    Dates are YYYY-MM-DD and the range is inclusive. `search` matches the
    description or merchant name. Omit account_id to search every account.
    fresh=true re-pulls one account from the bank first, so it needs account_id.
    """
    return _wrap(
        await service().transactions(
            _one_provider(provider_id) if (provider_id or fresh) else None,
            account_id,
            from_date=from_date,
            to_date=to_date,
            search=search,
            include_pending=include_pending,
            limit=limit,
            fresh=fresh,
        )
    )


@mcp.tool()
async def list_standing_orders(
    account_id: str, provider_id: str | None = None, fresh: bool = False
) -> dict:
    """List standing orders on an account (Open Banking providers only)."""
    return _wrap(
        await service().standing_orders(_one_provider(provider_id), account_id, fresh=fresh)
    )


@mcp.tool()
async def list_direct_debits(
    account_id: str, provider_id: str | None = None, fresh: bool = False
) -> dict:
    """List direct debits on an account (Open Banking providers only)."""
    return _wrap(
        await service().direct_debits(_one_provider(provider_id), account_id, fresh=fresh)
    )


@mcp.tool()
async def get_identity(provider_id: str | None = None, fresh: bool = False) -> dict:
    """Get the account holder's identity as the bank holds it."""
    return _wrap(await service().identity(_one_provider(provider_id), fresh=fresh))


@mcp.tool()
async def sync_now(provider_id: str | None = None, history_days: int | None = None) -> dict:
    """Pull everything from the bank into the local cache.

    Normally a scheduled job does this daily; call it when you need the whole
    cache refreshed rather than one account.
    """
    settings = _settings or load_settings()
    report = await service().sync(
        _one_provider(provider_id), history_days=history_days or settings.history_days
    )
    return {
        "provider_id": report.provider_id,
        "accounts": report.accounts,
        "cards": report.cards,
        "transactions": report.transactions,
        "pending": report.pending,
        "ok": report.ok,
        "errors": report.errors,
    }


@mcp.tool()
async def cache_status() -> dict:
    """Show what the local cache holds and when it was last refreshed."""
    svc = service()
    stats = svc._cache.stats()
    stats["last_run"] = svc._cache.last_run()
    return stats


def main() -> None:
    try:
        load_settings()
    except ConfigError as exc:
        raise SystemExit(f"open-banking-mcp: {exc}")
    mcp.run()


if __name__ == "__main__":
    main()
