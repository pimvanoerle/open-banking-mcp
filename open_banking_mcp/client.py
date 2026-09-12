"""Thin async wrapper over the TrueLayer Data API.

Knows nothing about caching -- every method here is a live HTTP call. The
caching policy lives in service.py.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx

from .auth import TokenManager
from .config import Settings


class TrueLayerError(RuntimeError):
    """A Data API request failed."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"{path} failed ({status}): {body[:300]}")
        self.status = status
        self.path = path


class RateLimited(TrueLayerError):
    """TrueLayer throttled us.

    Production limits are far tighter than the sandbox's (which has none).
    Requests without an X-PSU-IP header -- i.e. anything running unattended --
    get the strictest treatment, which is precisely what a scheduled sync is.
    """

    def __init__(self, path: str, body: str, retry_after: float | None) -> None:
        super().__init__(429, path, body)
        self.retry_after = retry_after

    def __str__(self) -> str:
        wait = (
            f" Retry after {self.retry_after:.0f}s."
            if self.retry_after
            else ""
        )
        return (
            f"Rate limited by TrueLayer on {self.path}.{wait} "
            f"Unattended calls are throttled hardest; set TRUELAYER_PSU_IP for "
            f"user-present requests, or sync less often."
        )


def _as_date(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


class TrueLayerClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        *,
        psu_ip: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        # TrueLayer throttles hard (~4 calls/day) when it can't tell that a
        # human is present. Passing the end user's IP lifts that, but it is
        # only honest to send it for user-initiated calls.
        self._psu_ip = psu_ip
        self._timeout = timeout

    async def _get(
        self,
        provider_id: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        paginate: bool = False,
    ) -> list[dict]:
        token = await self._tokens.access_token(provider_id)
        headers = {"Authorization": f"Bearer {token}"}
        if self._psu_ip:
            headers["X-PSU-IP"] = self._psu_ip
        if paginate:
            headers["tl-enable-pagination"] = "true"

        results: list[dict] = []
        query = dict(params or {})
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                response = await client.get(
                    f"{self._settings.api_base_url}{path}",
                    headers=headers,
                    params={k: v for k, v in query.items() if v is not None},
                )
                if response.status_code == 429:
                    header = response.headers.get("retry-after")
                    raise RateLimited(
                        path,
                        response.text,
                        float(header) if header and header.isdigit() else None,
                    )
                if response.status_code >= 400:
                    raise TrueLayerError(response.status_code, path, response.text)

                payload = response.json()
                results.extend(payload.get("results") or [])

                cursor = payload.get("next_cursor") or (payload.get("meta") or {}).get(
                    "next_cursor"
                )
                if not (paginate and cursor):
                    return results
                query["cursor"] = cursor

    # -- identity ---------------------------------------------------------

    async def info(self, provider_id: str) -> list[dict]:
        return await self._get(provider_id, "/info")

    async def metadata(self, provider_id: str) -> list[dict]:
        return await self._get(provider_id, "/me")

    # -- accounts ---------------------------------------------------------

    async def accounts(self, provider_id: str) -> list[dict]:
        return await self._get(provider_id, "/accounts")

    async def account_balance(self, provider_id: str, account_id: str) -> dict | None:
        results = await self._get(provider_id, f"/accounts/{account_id}/balance")
        return results[0] if results else None

    async def account_transactions(
        self,
        provider_id: str,
        account_id: str,
        *,
        from_date: date | str | None = None,
        to_date: date | str | None = None,
    ) -> list[dict]:
        return await self._get(
            provider_id,
            f"/accounts/{account_id}/transactions",
            params={"from": _as_date(from_date), "to": _as_date(to_date)},
            paginate=True,
        )

    async def pending_transactions(
        self, provider_id: str, account_id: str
    ) -> list[dict]:
        """Authorised but not yet settled -- the Wise-payment case."""
        return await self._get(
            provider_id, f"/accounts/{account_id}/transactions/pending"
        )

    async def standing_orders(self, provider_id: str, account_id: str) -> list[dict]:
        return await self._get(provider_id, f"/accounts/{account_id}/standing_orders")

    async def direct_debits(self, provider_id: str, account_id: str) -> list[dict]:
        return await self._get(provider_id, f"/accounts/{account_id}/direct_debits")

    # -- cards ------------------------------------------------------------

    async def cards(self, provider_id: str) -> list[dict]:
        return await self._get(provider_id, "/cards")

    async def card_balance(self, provider_id: str, card_id: str) -> dict | None:
        results = await self._get(provider_id, f"/cards/{card_id}/balance")
        return results[0] if results else None

    async def card_transactions(
        self,
        provider_id: str,
        card_id: str,
        *,
        from_date: date | str | None = None,
        to_date: date | str | None = None,
    ) -> list[dict]:
        return await self._get(
            provider_id,
            f"/cards/{card_id}/transactions",
            params={"from": _as_date(from_date), "to": _as_date(to_date)},
            paginate=True,
        )

    async def pending_card_transactions(
        self, provider_id: str, card_id: str
    ) -> list[dict]:
        return await self._get(provider_id, f"/cards/{card_id}/transactions/pending")
