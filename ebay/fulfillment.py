"""Read-only eBay Sell Fulfillment client."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from ebay.orders import EbayOrder, OrderResponseError, normalize_order
from ebay.seller_oauth import REQUEST_TIMEOUT_SECONDS, SellerOAuthClient


class FulfillmentApiError(RuntimeError):
    """A safe-to-display Fulfillment API failure."""


class FulfillmentRateLimitError(FulfillmentApiError):
    """eBay rate-limited the request."""


class FulfillmentClient:
    """Retrieve seller orders using only GET /sell/fulfillment/v1/order."""

    def __init__(
        self,
        oauth: SellerOAuthClient,
        *,
        session: requests.Session | None = None,
        page_size: int = 100,
    ) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        self._oauth = oauth
        self._session = session or requests.Session()
        self._page_size = page_size
        self._url = f"https://{oauth.config.api_host}/sell/fulfillment/v1/order"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("eBay timestamps must be timezone-aware")
        return (
            value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        """Extract only documented, non-sensitive fields from an eBay error payload."""
        try:
            body = response.json()
        except (ValueError, TypeError):
            return ""
        if not isinstance(body, dict) or not isinstance(body.get("errors"), list):
            return ""

        details = []
        for error in body["errors"][:3]:
            if not isinstance(error, dict):
                continue
            fields = []
            for key in ("errorId", "domain", "category", "message", "longMessage"):
                value = error.get(key)
                if isinstance(value, (str, int)):
                    text = str(value).replace("\r", " ").replace("\n", " ")[:500]
                    fields.append(f"{key}={text}")
            parameter_names = []
            if isinstance(error.get("parameters"), list):
                for parameter in error["parameters"]:
                    if isinstance(parameter, dict) and isinstance(parameter.get("name"), str):
                        parameter_names.append(parameter["name"][:100])
            if parameter_names:
                fields.append(f"parameters={','.join(parameter_names[:10])}")
            if fields:
                details.append("; ".join(fields))
        return " | ".join(details)

    def get_orders(self, start: datetime, end: datetime) -> list[EbayOrder]:
        if start >= end:
            raise ValueError("Order start date must be before end date")
        filter_value = f"creationdate:[{self._timestamp(start)}..{self._timestamp(end)}]"
        orders: list[EbayOrder] = []
        offset = 0
        retried_auth = False
        while True:
            token = self._oauth.access_token(force_refresh=retried_auth)
            try:
                response = self._session.get(
                    self._url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params={"filter": filter_value, "limit": self._page_size, "offset": offset},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise FulfillmentApiError(
                    "eBay order retrieval failed due to a network error"
                ) from exc
            if response.status_code == 401 and not retried_auth:
                retried_auth = True
                continue
            if response.status_code == 429:
                raise FulfillmentRateLimitError("eBay rate limit reached; try again later")
            if response.status_code == 403:
                raise FulfillmentApiError(
                    "eBay denied order access; verify seller authorization and Fulfillment scope"
                )
            if response.status_code >= 500:
                raise FulfillmentApiError(
                    f"eBay order service is temporarily unavailable (HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                detail = self._error_detail(response)
                suffix = f": {detail}" if detail else ""
                raise FulfillmentApiError(
                    f"eBay order retrieval failed (HTTP {response.status_code}){suffix}"
                )
            try:
                body: Any = response.json()
            except ValueError as exc:
                raise FulfillmentApiError("eBay returned malformed order data") from exc
            if not isinstance(body, dict) or not isinstance(body.get("orders", []), list):
                raise FulfillmentApiError("eBay returned malformed order data")
            try:
                page = [normalize_order(order) for order in body.get("orders", [])]
            except OrderResponseError as exc:
                raise FulfillmentApiError(str(exc)) from exc
            orders.extend(page)
            total = body.get("total")
            if not isinstance(total, int) or total < 0:
                raise FulfillmentApiError("eBay returned invalid pagination data")
            offset += len(page)
            if offset >= total:
                return orders
            if not page:
                raise FulfillmentApiError("eBay pagination ended before all orders were returned")
