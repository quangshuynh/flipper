"""Authenticated, read-only eBay Sell Finances client."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from ebay.finance_transactions import (
    FinanceResponseError,
    FinanceTransaction,
    normalize_transaction,
)
from ebay.fulfillment import FulfillmentClient
from ebay.seller_oauth import REQUEST_TIMEOUT_SECONDS, SellerOAuthClient


class FinancesApiError(RuntimeError):
    """A safe-to-display Finances API failure."""


class FinancesRateLimitError(FinancesApiError):
    """eBay rate-limited the request."""


class FinancesClient:
    """Retrieve transactions using only GET /sell/finances/v1/transaction."""

    def __init__(self, oauth: SellerOAuthClient, *, session=None, page_size: int = 1000) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        self._oauth = oauth
        self._session = session or requests.Session()
        self._page_size = page_size
        host = (
            "apiz.ebay.com" if oauth.config.environment == "production" else "apiz.sandbox.ebay.com"
        )
        self._url = f"https://{host}/sell/finances/v1/transaction"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("eBay timestamps must be timezone-aware")
        return (
            value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

    def get_transactions(self, start: datetime, end: datetime) -> list[FinanceTransaction]:
        if start >= end:
            raise ValueError("Finance start date must be before end date")
        date_filter = f"transactionDate:[{self._timestamp(start)}..{self._timestamp(end)}]"
        transactions: list[FinanceTransaction] = []
        offset = 0
        retried_auth = False
        while True:
            token = self._oauth.access_token(force_refresh=retried_auth)
            try:
                response = self._session.get(
                    self._url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params={"filter": date_filter, "limit": self._page_size, "offset": offset},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise FinancesApiError(
                    "eBay finance retrieval failed due to a network error"
                ) from exc
            if response.status_code == 401 and not retried_auth:
                retried_auth = True
                continue
            if response.status_code == 204:
                return transactions
            if response.status_code == 429:
                raise FinancesRateLimitError("eBay rate limit reached; try again later")
            if response.status_code == 403:
                raise FinancesApiError(
                    "eBay denied Finances access; reconnect the seller account for Finances scope"
                )
            if response.status_code >= 500:
                raise FinancesApiError(
                    f"eBay finance service is temporarily unavailable (HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                detail = FulfillmentClient._error_detail(response)
                suffix = f": {detail}" if detail else ""
                raise FinancesApiError(
                    f"eBay finance retrieval failed (HTTP {response.status_code}){suffix}"
                )
            try:
                body: Any = response.json()
            except ValueError as exc:
                raise FinancesApiError("eBay returned malformed finance data") from exc
            if not isinstance(body, dict) or not isinstance(body.get("transactions", []), list):
                raise FinancesApiError("eBay returned malformed finance data")
            try:
                page = [normalize_transaction(item) for item in body.get("transactions", [])]
            except FinanceResponseError as exc:
                raise FinancesApiError(str(exc)) from exc
            transactions.extend(page)
            total = body.get("total")
            if not isinstance(total, int) or total < 0:
                raise FinancesApiError("eBay returned invalid pagination data")
            offset += len(page)
            if offset >= total:
                return transactions
            if not page:
                raise FinancesApiError(
                    "eBay pagination ended before all transactions were returned"
                )
