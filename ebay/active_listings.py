"""Read-only Trading API client for the authenticated seller's active listings."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

import requests

from ebay.listings import EbayActiveListing, ListingResponseError, normalize_listing
from ebay.seller_oauth import REQUEST_TIMEOUT_SECONDS, SellerOAuthClient

TRADING_API_VERSION = "1477"
NS = "urn:ebay:apis:eBLBaseComponents"


class ActiveListingsApiError(RuntimeError):
    """A safe-to-display active-listing retrieval failure."""


class ActiveListingsClient:
    """Execute only the Trading API GetMyeBaySelling read operation."""

    def __init__(self, oauth: SellerOAuthClient, *, session=None, page_size: int = 200) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        self._oauth = oauth
        self._session = session or requests.Session()
        self._page_size = page_size
        host = (
            "api.ebay.com" if oauth.config.environment == "production" else "api.sandbox.ebay.com"
        )
        self._url = f"https://{host}/ws/api.dll"

    def _request_xml(self, page: int) -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<GetMyeBaySellingRequest xmlns="{NS}">'
            "<ActiveList><Include>true</Include><IncludeNotes>false</IncludeNotes>"
            f"<Pagination><EntriesPerPage>{self._page_size}</EntriesPerPage>"
            f"<PageNumber>{page}</PageNumber></Pagination></ActiveList>"
            "<HideVariations>false</HideVariations>"
            "</GetMyeBaySellingRequest>"
        ).encode()

    def get_active_listings(self) -> list[EbayActiveListing]:
        listings: list[EbayActiveListing] = []
        page = 1
        retried_auth = False
        while True:
            token = self._oauth.access_token(force_refresh=retried_auth)
            headers = {
                "X-EBAY-API-IAF-TOKEN": token,
                "Content-Type": "text/xml",
                "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
                "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_API_VERSION,
                "X-EBAY-API-SITEID": "0",
            }
            try:
                response = self._session.post(
                    self._url,
                    headers=headers,
                    data=self._request_xml(page),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise ActiveListingsApiError(
                    "eBay active-listing retrieval failed due to a network error"
                ) from exc
            if response.status_code == 401 and not retried_auth:
                retried_auth = True
                continue
            if response.status_code in {401, 403}:
                detail = self._error_detail(response.content, secrets=(token,))
                suffix = f": {detail}" if detail else ""
                raise ActiveListingsApiError(
                    f"eBay active-listing token or scope was rejected "
                    f"(HTTP {response.status_code}){suffix}; reconnect with "
                    "'python main.py ebay connect' if authorization changed"
                )
            if response.status_code == 429:
                raise ActiveListingsApiError("eBay rate limit reached; try again later")
            if response.status_code >= 500:
                raise ActiveListingsApiError(
                    f"eBay listing service is temporarily unavailable (HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                detail = self._error_detail(response.content, secrets=(token,))
                suffix = f": {detail}" if detail else ""
                raise ActiveListingsApiError(
                    f"eBay active-listing transport rejection (HTTP {response.status_code}){suffix}"
                )
            parsed, total_pages = self._parse(response.content, secrets=(token,))
            listings.extend(parsed)
            if page >= total_pages:
                return listings
            page += 1

    @staticmethod
    def _safe_text(
        element: ElementTree.Element | None, tag: str, secrets: tuple[str, ...] = ()
    ) -> str | None:
        if element is None:
            return None
        value = element.findtext(f"{{{NS}}}{tag}")
        if value is None:
            return None
        cleaned = " ".join(value.split())
        for secret in secrets:
            if secret:
                cleaned = cleaned.replace(secret, "[REDACTED]")
        cleaned = cleaned[:500]
        return cleaned or None

    @classmethod
    def _error_detail(cls, payload: bytes, *, secrets: tuple[str, ...] = ()) -> str:
        """Extract only allowlisted Trading error fields, never the raw response."""
        try:
            root = ElementTree.fromstring(payload)
        except (ElementTree.ParseError, TypeError):
            return ""
        fields = []
        ack = cls._safe_text(root, "Ack", secrets)
        if ack:
            fields.append(f"ack={ack}")
        for error in root.findall(f"{{{NS}}}Errors")[:3]:
            for tag, label in (
                ("ErrorCode", "errorCode"),
                ("SeverityCode", "severity"),
                ("ErrorClassification", "classification"),
                ("ShortMessage", "shortMessage"),
                ("LongMessage", "longMessage"),
            ):
                value = cls._safe_text(error, tag, secrets)
                if value:
                    fields.append(f"{label}={value}")
        return "; ".join(fields)

    @staticmethod
    def _parse(
        payload: bytes, *, secrets: tuple[str, ...] = ()
    ) -> tuple[list[EbayActiveListing], int]:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise ActiveListingsApiError("eBay returned malformed active-listing data") from exc
        ack = root.findtext(f"{{{NS}}}Ack")
        if ack not in {"Success", "Warning"}:
            detail = ActiveListingsClient._error_detail(payload, secrets=secrets)
            suffix = f": {detail}" if detail else ""
            raise ActiveListingsApiError(
                f"eBay Trading API rejected active-listing request{suffix}"
            )
        active = root.find(f"{{{NS}}}ActiveList")
        if active is None:
            return [], 0
        raw_pages = active.findtext(f"{{{NS}}}PaginationResult/{{{NS}}}TotalNumberOfPages")
        try:
            total_pages = int(raw_pages or "0")
        except ValueError as exc:
            raise ActiveListingsApiError("eBay returned invalid listing pagination data") from exc
        result = []
        for item in active.findall(f"{{{NS}}}ItemArray/{{{NS}}}Item"):

            def find(path: str) -> str | None:
                return item.findtext("/".join(f"{{{NS}}}{part}" for part in path.split("/")))

            price = item.find(f"{{{NS}}}SellingStatus/{{{NS}}}CurrentPrice")
            raw: dict[str, Any] = {
                "item_id": find("ItemID"),
                "sku": find("SKU"),
                "title": find("Title"),
                # Membership in GetMyeBaySelling.ActiveList is the authoritative status.
                "status": find("SellingStatus/ListingStatus") or "Active",
                "price_value": price.text if price is not None else None,
                "price_currency": price.get("currencyID") if price is not None else None,
                "quantity_available": find("QuantityAvailable"),
                "listing_url": find("ListingDetails/ViewItemURL"),
                "started_at": find("ListingDetails/StartTime"),
                "ends_at": find("ListingDetails/EndTime"),
            }
            try:
                result.append(normalize_listing(raw))
            except ListingResponseError as exc:
                raise ActiveListingsApiError(str(exc)) from exc
        return result, total_pages
