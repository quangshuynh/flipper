from decimal import Decimal

import pytest
import requests

from deals.categories import DealCategory
from deals.ebay import (
    map_ebay_category,
    map_ebay_category_mapping,
    normalize_ebay_item,
    normalize_search_results,
)
from deals.models import AmountStatus, EvidenceLevel, ProvenanceKind, SourceIdentity
from ebay.discovery import (
    BROWSE_SCOPE,
    DiscoveryConfig,
    EbayDiscoveryClient,
    EbayDiscoveryError,
    MAX_RESULTS,
)


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body if body is not None else {}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _client(*gets, environment="production"):
    session = Session(
        posts=[Response(body={"access_token": "private-token", "expires_in": 7200})],
        gets=gets,
    )
    return EbayDiscoveryClient(
        DiscoveryConfig(environment, "application-id", "application-secret"), session=session
    ), session


def _item(**updates):
    item = {
        "itemId": "v1|123|0",
        "title": "Test camera",
        "price": {"value": "54.25", "currency": "USD"},
        "categoryId": "293",
        "condition": "Used",
        "itemWebUrl": "https://www.ebay.com/itm/123",
        "itemLocation": {"city": "Boston", "stateOrProvince": "MA", "country": "US"},
        "shippingOptions": [{"shippingCost": {"value": "8.50", "currency": "USD"}}],
    }
    item.update(updates)
    return item


def test_application_oauth_is_separate_and_environment_specific():
    client, session = _client(Response(body={"itemSummaries": []}), environment="sandbox")
    client.search("camera")
    token_url, token_request = session.post_calls[0]
    search_url, request = session.get_calls[0]
    assert token_url.startswith("https://api.sandbox.ebay.com/")
    assert search_url.startswith("https://api.sandbox.ebay.com/")
    assert token_request["data"] == {"grant_type": "client_credentials", "scope": BROWSE_SCOPE}
    assert request["headers"]["Authorization"] == "Bearer private-token"
    assert "refresh_token" not in str(token_request)


def test_search_constructs_bounded_filters_and_pagination():
    client, session = _client(Response(body={"itemSummaries": []}))
    client.search(
        "camera bag",
        category_id="293",
        minimum_price="10",
        maximum_price="50",
        condition="USED",
        limit=MAX_RESULTS,
        offset=50,
    )
    params = session.get_calls[0][1]["params"]
    assert params["q"] == "camera bag"
    assert params["category_ids"] == "293"
    assert params["limit"] == 50 and params["offset"] == 50
    assert "price:[10..50]" in params["filter"]
    assert "conditions:{USED}" in params["filter"]
    with pytest.raises(ValueError):
        client.search("")
    with pytest.raises(ValueError):
        client.search("camera", limit=51)


def test_normalization_preserves_source_money_shipping_and_unknowns():
    opportunity, shipping = normalize_ebay_item(_item())
    assert opportunity.source is SourceIdentity.EBAY
    assert opportunity.source_listing_id == "v1|123|0"
    assert opportunity.category is DealCategory.ELECTRONICS
    assert opportunity.base_price.amount == Decimal("54.25")
    assert opportunity.base_price.currency == "USD"
    assert shipping.money.amount == Decimal("8.50")
    assert opportunity.url == "https://www.ebay.com/itm/123"
    assert opportunity.location_text == "Boston, MA, US"
    assert not hasattr(opportunity, "expected_resale")
    _, unknown_shipping = normalize_ebay_item(_item(shippingOptions=[]))
    assert unknown_shipping.status is AmountStatus.UNKNOWN


def test_mapping_fallback_safe_urls_and_partial_malformed_results():
    assert map_ebay_category("6000") is DealCategory.VEHICLE_PARTS
    assert map_ebay_category("unknown") is DealCategory.EVERYTHING_ELSE
    valid = _item(itemWebUrl="https://evil.example/steal")
    opportunity, _ = normalize_ebay_item(valid)
    assert opportunity.url is None
    results = normalize_search_results({"itemSummaries": [valid, {}, "bad"]})
    assert len(results) == 1


def test_taxonomy_ancestry_maps_leaf_and_preserves_provenance():
    mapping = map_ebay_category_mapping("31388", ("0", "293"))
    assert mapping.category is DealCategory.ELECTRONICS
    assert mapping.confidence is EvidenceLevel.STRONG
    assert mapping.provenance.kind is ProvenanceKind.SOURCE_API
    opportunity, _ = normalize_ebay_item(
        _item(categoryId="31388"), category_ancestry={"31388": ("0", "293")}
    )
    assert opportunity.category is DealCategory.ELECTRONICS
    assert opportunity.category_provenance.label == "eBay Taxonomy ancestry"


def test_marketplace_specific_and_unknown_mapping_fall_back_without_false_confidence():
    mapping = map_ebay_category_mapping("293", marketplace_id="EBAY_GB")
    assert mapping.category is DealCategory.EVERYTHING_ELSE
    assert mapping.confidence is None
    assert mapping.provenance.kind is ProvenanceKind.UNKNOWN


def test_taxonomy_tree_is_normalized_once_and_cached_without_n_plus_one():
    EbayDiscoveryClient.clear_taxonomy_cache()
    tree_info = Response(body={"categoryTreeId": "0", "categoryTreeVersion": "123"})
    tree = Response(
        body={
            "categoryTreeVersion": "123",
            "rootCategoryNode": {
                "category": {"categoryId": "0", "categoryName": "Root"},
                "childCategoryTreeNodes": [
                    {
                        "category": {"categoryId": "293", "categoryName": "Electronics"},
                        "childCategoryTreeNodes": [
                            {
                                "category": {"categoryId": "31388", "categoryName": "Cameras"},
                                "leafCategoryTreeNode": True,
                            }
                        ],
                    }
                ],
            },
        }
    )
    client, session = _client(tree_info, tree)
    first = client.category_ancestry()
    second = client.category_ancestry()
    assert first["31388"] == ("0", "293")
    assert second is first
    assert len(session.get_calls) == 2


@pytest.mark.parametrize(
    "responses",
    [
        (Response(body={"unexpected": "shape"}),),
        (
            Response(body={"categoryTreeId": "0", "categoryTreeVersion": "1"}),
            Response(body={"categoryTreeVersion": "1", "rootCategoryNode": []}),
        ),
    ],
)
def test_taxonomy_malformed_responses_are_sanitized(responses):
    EbayDiscoveryClient.clear_taxonomy_cache()
    client, _ = _client(*responses)
    with pytest.raises(EbayDiscoveryError, match="taxonomy returned malformed"):
        client.category_ancestry()


def test_failures_are_sanitized_and_tokens_never_leak():
    client, _ = _client(Response(status=429), Response(status=429))
    with pytest.raises(EbayDiscoveryError, match="rate limit") as error:
        client.search("camera")
    assert "private-token" not in str(error.value)
    client, _ = _client(requests.Timeout("private-token"))
    with pytest.raises(EbayDiscoveryError, match="timed out") as error:
        client.search("camera")
    assert "private-token" not in str(error.value)
    client, _ = _client(Response(body=ValueError("private-token")))
    with pytest.raises(EbayDiscoveryError, match="malformed") as error:
        client.search("camera")
    assert "private-token" not in str(error.value)
