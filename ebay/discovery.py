"""Read-only public eBay Browse discovery with application OAuth."""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import requests

BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RESULTS = 50
TAXONOMY_CACHE_SECONDS = 24 * 60 * 60
MAX_TAXONOMY_CACHE_ENTRIES = 8


class EbayDiscoveryError(RuntimeError):
    """A sanitized, safe-to-display discovery failure."""


@dataclass(frozen=True)
class DiscoveryConfig:
    environment: str
    client_id: str
    client_secret: str
    marketplace_id: str = "EBAY_US"

    @classmethod
    def from_environment(cls) -> DiscoveryConfig:
        environment = os.getenv("EBAY_DISCOVERY_ENV", "production").strip().lower()
        if environment not in {"production", "sandbox"}:
            raise EbayDiscoveryError("EBAY_DISCOVERY_ENV must be 'production' or 'sandbox'")
        client_id = os.getenv("EBAY_DISCOVERY_CLIENT_ID", "").strip()
        client_secret = os.getenv("EBAY_DISCOVERY_CLIENT_SECRET", "").strip()
        marketplace = os.getenv("EBAY_DISCOVERY_MARKETPLACE", "EBAY_US").strip().upper()
        if not client_id or not client_secret:
            raise EbayDiscoveryError("eBay discovery is not configured")
        return cls(environment, client_id, client_secret, marketplace)

    @property
    def api_host(self) -> str:
        return "api.ebay.com" if self.environment == "production" else "api.sandbox.ebay.com"


class EbayDiscoveryClient:
    """Search public listings using an Application token, never seller authorization."""

    _taxonomy_cache: OrderedDict[tuple[str, str], tuple[float, str, dict[str, tuple[str, ...]]]] = (
        OrderedDict()
    )

    def __init__(self, config: DiscoveryConfig, *, session=None, clock=time.monotonic) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        if self._token and self.clock() < self._expires_at:
            return self._token
        try:
            response = self.session.post(
                f"https://{self.config.api_host}/identity/v1/oauth2/token",
                auth=(self.config.client_id, self.config.client_secret),
                data={"grant_type": "client_credentials", "scope": BROWSE_SCOPE},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EbayDiscoveryError(
                "eBay discovery authentication is temporarily unavailable"
            ) from exc
        if response.status_code >= 400:
            raise EbayDiscoveryError(
                f"eBay discovery authentication was rejected (HTTP {response.status_code})"
            )
        try:
            token, expires = body["access_token"], int(body["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EbayDiscoveryError(
                "eBay discovery authentication returned malformed data"
            ) from exc
        if not isinstance(token, str) or not token or expires <= 0:
            raise EbayDiscoveryError("eBay discovery authentication returned malformed data")
        self._token = token
        self._expires_at = self.clock() + max(0, expires - 60)
        return token

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
        }
        for attempt in range(2):
            try:
                response = self.session.get(
                    f"https://{self.config.api_host}{path}",
                    headers=headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.Timeout as exc:
                raise EbayDiscoveryError("eBay discovery timed out; try again") from exc
            except requests.RequestException as exc:
                raise EbayDiscoveryError("eBay discovery is temporarily unavailable") from exc
            if response.status_code == 401 and attempt == 0:
                self._token = None
                headers["Authorization"] = f"Bearer {self.access_token()}"
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                continue
            break
        if response.status_code == 429:
            raise EbayDiscoveryError("eBay discovery rate limit reached; try again later")
        if response.status_code >= 400:
            raise EbayDiscoveryError(f"eBay discovery failed (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise EbayDiscoveryError("eBay discovery returned malformed data") from exc
        if not isinstance(body, dict):
            raise EbayDiscoveryError("eBay discovery returned malformed data")
        return body

    def search(
        self,
        keywords: str,
        *,
        category_id: str | None = None,
        minimum_price: str | None = None,
        maximum_price: str | None = None,
        condition: str | None = None,
        limit: int = 24,
        offset: int = 0,
    ) -> dict[str, Any]:
        keywords = keywords.strip()
        if not keywords or len(keywords) > 100:
            raise ValueError("keywords must contain 1 to 100 characters")
        if not 1 <= limit <= MAX_RESULTS or offset < 0 or offset > 10_000:
            raise ValueError("result bounds are invalid")
        if category_id and (not category_id.isdigit() or len(category_id) > 12):
            raise ValueError("category identity is invalid")
        if condition and condition not in {"NEW", "USED", "CERTIFIED_REFURBISHED"}:
            raise ValueError("condition is invalid")
        filters = ["buyingOptions:{FIXED_PRICE}"]
        if minimum_price or maximum_price:
            try:
                low_value = Decimal(minimum_price or "0")
                high_value = Decimal(maximum_price) if maximum_price else None
            except InvalidOperation as exc:
                raise ValueError("price filters must be exact decimal amounts") from exc
            if low_value < 0 or (high_value is not None and high_value < low_value):
                raise ValueError("price range is invalid")
            low, high = minimum_price or "0", maximum_price or "*"
            filters.extend([f"price:[{low}..{high}]", "priceCurrency:USD"])
        if condition:
            filters.append(f"conditions:{{{condition}}}")
        params: dict[str, Any] = {
            "q": keywords,
            "limit": limit,
            "offset": offset,
            "filter": ",".join(filters),
            "fieldgroups": "EXTENDED",
        }
        if category_id:
            params["category_ids"] = category_id
        return self._get("/buy/browse/v1/item_summary/search", params=params)

    def get_item(self, item_id: str) -> dict[str, Any]:
        if not item_id or len(item_id) > 200 or any(char.isspace() for char in item_id):
            raise ValueError("invalid eBay item identity")
        return self._get(f"/buy/browse/v1/item/{quote(item_id, safe='|')}")

    @classmethod
    def clear_taxonomy_cache(cls) -> None:
        cls._taxonomy_cache.clear()

    def category_ancestry(self) -> dict[str, tuple[str, ...]]:
        """Build a bounded, version-aware ancestry index without per-item requests."""
        cache_key = (self.config.environment, self.config.marketplace_id)
        cached = self._taxonomy_cache.get(cache_key)
        now = self.clock()
        if cached and now - cached[0] < TAXONOMY_CACHE_SECONDS:
            self._taxonomy_cache.move_to_end(cache_key)
            return cached[2]
        tree_info = self._get(
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            params={"marketplace_id": self.config.marketplace_id},
        )
        tree_id = tree_info.get("categoryTreeId")
        version = tree_info.get("categoryTreeVersion")
        if (
            not isinstance(tree_id, str)
            or not tree_id
            or not isinstance(version, str)
            or not version
        ):
            raise EbayDiscoveryError("eBay taxonomy returned malformed data")
        if cached and cached[1] == version:
            self._taxonomy_cache[cache_key] = (now, version, cached[2])
            return cached[2]
        body = self._get(f"/commerce/taxonomy/v1/category_tree/{quote(tree_id, safe='')}")
        if body.get("categoryTreeVersion") != version:
            raise EbayDiscoveryError("eBay taxonomy returned inconsistent data")
        index: dict[str, tuple[str, ...]] = {}

        def visit(node: Any, ancestors: tuple[str, ...] = ()) -> None:
            if not isinstance(node, dict):
                raise EbayDiscoveryError("eBay taxonomy returned malformed data")
            category = node.get("category")
            if not isinstance(category, dict) or not isinstance(category.get("categoryId"), str):
                raise EbayDiscoveryError("eBay taxonomy returned malformed data")
            category_id = category["categoryId"]
            index[category_id] = ancestors
            children = node.get("childCategoryTreeNodes", [])
            if not isinstance(children, list):
                raise EbayDiscoveryError("eBay taxonomy returned malformed data")
            for child in children:
                visit(child, ancestors + (category_id,))

        visit(body.get("rootCategoryNode"))
        self._taxonomy_cache[cache_key] = (now, version, index)
        self._taxonomy_cache.move_to_end(cache_key)
        while len(self._taxonomy_cache) > MAX_TAXONOMY_CACHE_ENTRIES:
            self._taxonomy_cache.popitem(last=False)
        return index
