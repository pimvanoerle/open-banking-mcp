"""Cache-or-fetch policy, and the daily sync.

Reads are served from the local cache by default. `fresh=True` bypasses it,
hits the API and writes the result back -- for the "I just sent a payment, did
it land?" case.

Every result carries `as_of` and `stale`, so a caller reporting a balance can
say how old it is instead of implying it is live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .cache import Cache
from .client import TrueLayerClient, TrueLayerError

# How far back a sync pulls on a first run.
DEFAULT_HISTORY_DAYS = 365
# Beyond this, cached data is flagged stale in the response.
DEFAULT_MAX_AGE = timedelta(hours=25)  # a daily sync plus an hour of slack


@dataclass
class Result:
    """Data plus its provenance."""

    data: Any
    as_of: datetime | None
    source: str  # 'cache' | 'live'
    max_age: timedelta = DEFAULT_MAX_AGE

    @property
    def age(self) -> timedelta | None:
        if self.as_of is None:
            return None
        return datetime.now(timezone.utc) - self.as_of

    @property
    def stale(self) -> bool:
        if self.source == "live":
            return False
        age = self.age
        return age is None or age > self.max_age

    def describe_age(self) -> str:
        if self.source == "live":
            return "live"
        if self.as_of is None:
            return "never synced"
        age = self.age
        if age < timedelta(minutes=2):
            return "just synced"
        if age < timedelta(hours=1):
            return f"{int(age.total_seconds() // 60)} minutes old"
        if age < timedelta(days=1):
            return f"{int(age.total_seconds() // 3600)} hours old"
        return f"{age.days} days old"

    def envelope(self) -> dict:
        return {
            "data": self.data,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "age": self.describe_age(),
            "source": self.source,
            "stale": self.stale,
        }


@dataclass
class SyncReport:
    provider_id: str
    accounts: int = 0
    cards: int = 0
    transactions: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class DataService:
    def __init__(
        self,
        client: TrueLayerClient,
        cache: Cache,
        *,
        max_age: timedelta = DEFAULT_MAX_AGE,
    ) -> None:
        self._client = client
        self._cache = cache
        self._max_age = max_age

    async def _snapshot(
        self, provider_id: str, kind: str, fetch, *, key: str = "", fresh: bool = False
    ) -> Result:
        if not fresh:
            cached = self._cache.get_snapshot(provider_id, kind, key)
            if cached is not None:
                payload, synced_at = cached
                return Result(payload, synced_at, "cache", self._max_age)
            # Nothing cached yet -- fall through and fetch rather than return
            # an empty answer that looks like "you have no accounts".

        payload = await fetch()
        self._cache.put_snapshot(provider_id, kind, payload, key)
        return Result(payload, datetime.now(timezone.utc), "live", self._max_age)

    # -- reads -------------------------------------------------------------

    async def accounts(self, provider_id: str, *, fresh: bool = False) -> Result:
        return await self._snapshot(
            provider_id, "accounts", lambda: self._client.accounts(provider_id), fresh=fresh
        )

    async def cards(self, provider_id: str, *, fresh: bool = False) -> Result:
        return await self._snapshot(
            provider_id, "cards", lambda: self._client.cards(provider_id), fresh=fresh
        )

    async def identity(self, provider_id: str, *, fresh: bool = False) -> Result:
        return await self._snapshot(
            provider_id, "info", lambda: self._client.info(provider_id), fresh=fresh
        )

    async def balance(
        self, provider_id: str, account_id: str, *, fresh: bool = False
    ) -> Result:
        return await self._snapshot(
            provider_id,
            "balance",
            lambda: self._client.account_balance(provider_id, account_id),
            key=account_id,
            fresh=fresh,
        )

    async def card_balance(
        self, provider_id: str, card_id: str, *, fresh: bool = False
    ) -> Result:
        return await self._snapshot(
            provider_id,
            "card_balance",
            lambda: self._client.card_balance(provider_id, card_id),
            key=card_id,
            fresh=fresh,
        )

    async def standing_orders(
        self, provider_id: str, account_id: str, *, fresh: bool = False
    ) -> Result:
        return await self._snapshot(
            provider_id,
            "standing_orders",
            lambda: self._client.standing_orders(provider_id, account_id),
            key=account_id,
            fresh=fresh,
        )

    async def direct_debits(
        self, provider_id: str, account_id: str, *, fresh: bool = False
    ) -> Result:
        return await self._snapshot(
            provider_id,
            "direct_debits",
            lambda: self._client.direct_debits(provider_id, account_id),
            key=account_id,
            fresh=fresh,
        )

    async def transactions(
        self,
        provider_id: str | None = None,
        holder_id: str | None = None,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        search: str | None = None,
        include_pending: bool = True,
        limit: int = 200,
        fresh: bool = False,
    ) -> Result:
        if fresh:
            if not (provider_id and holder_id):
                raise ValueError(
                    "fresh=True needs both provider_id and holder_id -- a live "
                    "refresh is per-account, not across every account at once."
                )
            await self._refresh_transactions(
                provider_id, holder_id, from_date=from_date, to_date=to_date
            )

        rows, synced_at = self._cache.transactions(
            provider_id,
            holder_id,
            from_date=from_date,
            to_date=to_date,
            search=search,
            include_pending=include_pending,
            limit=limit,
        )
        return Result(rows, synced_at, "live" if fresh else "cache", self._max_age)

    async def _refresh_transactions(
        self,
        provider_id: str,
        holder_id: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> None:
        """Pull one holder's recent transactions live and fold them into cache."""
        kind = await self._holder_kind(provider_id, holder_id)
        start = from_date or (date.today() - timedelta(days=30)).isoformat()
        end = to_date or date.today().isoformat()

        if kind == "card":
            settled = await self._client.card_transactions(
                provider_id, holder_id, from_date=start, to_date=end
            )
            pending = await self._client.pending_card_transactions(provider_id, holder_id)
        else:
            settled = await self._client.account_transactions(
                provider_id, holder_id, from_date=start, to_date=end
            )
            pending = await self._client.pending_transactions(provider_id, holder_id)

        self._cache.put_transactions(provider_id, kind, holder_id, settled)
        self._cache.put_transactions(
            provider_id, kind, holder_id, pending, is_pending=True
        )

    async def _holder_kind(self, provider_id: str, holder_id: str) -> str:
        """Is this id an account or a card? Answered from cache where possible."""
        cached = self._cache.get_snapshot(provider_id, "cards")
        if cached:
            cards, _ = cached
            if any(c.get("account_id") == holder_id for c in cards or []):
                return "card"
            return "account"
        cards = await self._client.cards(provider_id)
        self._cache.put_snapshot(provider_id, "cards", cards)
        return "card" if any(c.get("account_id") == holder_id for c in cards) else "account"

    # -- sync --------------------------------------------------------------

    async def sync(
        self, provider_id: str, *, history_days: int = DEFAULT_HISTORY_DAYS
    ) -> SyncReport:
        """Pull everything for one provider into the cache."""
        report = SyncReport(provider_id)
        run_id = self._cache.start_run(provider_id)
        start = (date.today() - timedelta(days=history_days)).isoformat()
        end = date.today().isoformat()

        try:
            for kind, fetch in (
                ("info", self._client.info(provider_id)),
                ("accounts", self._client.accounts(provider_id)),
                ("cards", self._client.cards(provider_id)),
            ):
                try:
                    self._cache.put_snapshot(provider_id, kind, await fetch)
                except TrueLayerError as exc:
                    report.errors.append(f"{kind}: {exc}")

            accounts = (self._cache.get_snapshot(provider_id, "accounts") or ([], None))[0]
            cards = (self._cache.get_snapshot(provider_id, "cards") or ([], None))[0]
            report.accounts, report.cards = len(accounts or []), len(cards or [])

            for holder_kind, holders in (("account", accounts or []), ("card", cards or [])):
                for holder in holders:
                    hid = holder.get("account_id")
                    if not hid:
                        continue
                    report_counts = await self._sync_holder(
                        provider_id, holder_kind, hid, start, end, report
                    )
                    report.transactions += report_counts[0]
                    report.pending += report_counts[1]

            self._cache.finish_run(
                run_id,
                "ok" if report.ok else "error",
                "; ".join(report.errors) or None,
            )
        except Exception as exc:
            self._cache.finish_run(run_id, "error", str(exc))
            raise
        return report

    async def _sync_holder(
        self, provider_id: str, kind: str, hid: str, start: str, end: str,
        report: SyncReport,
    ) -> tuple[int, int]:
        settled_n = pending_n = 0

        bal_kind = "card_balance" if kind == "card" else "balance"
        bal_call = (
            self._client.card_balance if kind == "card" else self._client.account_balance
        )
        try:
            self._cache.put_snapshot(
                provider_id, bal_kind, await bal_call(provider_id, hid), key=hid
            )
        except TrueLayerError as exc:
            report.errors.append(f"{bal_kind}[{hid[:8]}]: {exc}")

        tx_call = (
            self._client.card_transactions if kind == "card"
            else self._client.account_transactions
        )
        try:
            rows = await tx_call(provider_id, hid, from_date=start, to_date=end)
            self._cache.put_transactions(provider_id, kind, hid, rows)
            settled_n = len(rows)
        except TrueLayerError as exc:
            report.errors.append(f"transactions[{hid[:8]}]: {exc}")

        pend_call = (
            self._client.pending_card_transactions if kind == "card"
            else self._client.pending_transactions
        )
        try:
            rows = await pend_call(provider_id, hid)
            self._cache.put_transactions(provider_id, kind, hid, rows, is_pending=True)
            pending_n = len(rows)
        except TrueLayerError as exc:
            report.errors.append(f"pending[{hid[:8]}]: {exc}")

        if kind == "account":
            for extra, call in (
                ("standing_orders", self._client.standing_orders),
                ("direct_debits", self._client.direct_debits),
            ):
                try:
                    self._cache.put_snapshot(
                        provider_id, extra, await call(provider_id, hid), key=hid
                    )
                except TrueLayerError as exc:
                    report.errors.append(f"{extra}[{hid[:8]}]: {exc}")

        return settled_n, pending_n
