from decimal import Decimal

import pytest

from ebay.active_listings import ActiveListingsApiError, ActiveListingsClient


class OAuth:
    class Config:
        environment = "production"

    config = Config()

    def __init__(self):
        self.forces = []

    def access_token(self, force_refresh=False):
        self.forces.append(force_refresh)
        return "secret"


class Response:
    def __init__(self, body=b"", status=200):
        self.content = body
        self.status_code = status


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def xml(page=1, pages=1, item_id="137744631273", sku="Q0001"):
    sku_xml = f"<SKU>{sku}</SKU>" if sku is not None else ""
    return f"""<?xml version="1.0"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack><ActiveList><ItemArray><Item>
        <ItemID>{item_id}</ItemID>{sku_xml}<Title>Recorder</Title>
        <QuantityAvailable>1</QuantityAvailable>
        <SellingStatus><CurrentPrice currencyID="USD">75.00</CurrentPrice>
        <ListingStatus>Active</ListingStatus></SellingStatus>
        <ListingDetails><ViewItemURL>https://www.ebay.com/itm/{item_id}</ViewItemURL><StartTime>2026-09-01T00:00:00Z</StartTime></ListingDetails>
      </Item></ItemArray><PaginationResult><TotalNumberOfPages>{pages}</TotalNumberOfPages></PaginationResult></ActiveList>
    </GetMyeBaySellingResponse>""".encode()


def test_read_operation_normalizes_minimal_fields_and_discards_payload():
    session = Session([Response(xml())])
    listing = ActiveListingsClient(OAuth(), session=session).get_active_listings()[0]
    assert listing.item_id == "137744631273"
    assert listing.sku == "Q0001"
    assert listing.asking_price.value == Decimal("75.00")
    url, request = session.calls[0]
    assert url == "https://api.ebay.com/ws/api.dll"
    assert request["headers"]["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling"
    assert request["headers"]["X-EBAY-API-IAF-TOKEN"] == "secret"
    assert "Authorization" not in request["headers"]
    assert request["headers"]["X-EBAY-API-COMPATIBILITY-LEVEL"] == "1477"
    assert request["headers"]["X-EBAY-API-SITEID"] == "0"
    assert request["headers"]["Content-Type"] == "text/xml"
    assert b'xmlns="urn:ebay:apis:eBLBaseComponents"' in request["data"]
    assert b"<ActiveList>" in request["data"]
    assert b"<Include>true</Include>" in request["data"]
    assert b"<EntriesPerPage>200</EntriesPerPage>" in request["data"]
    assert b"<PageNumber>1</PageNumber>" in request["data"]
    assert b"SoldList" not in request["data"]


def test_pagination_and_auth_retry():
    oauth = OAuth()
    session = Session(
        [Response(status=401), Response(xml(1, 2)), Response(xml(2, 2, "2", "Q0002"))]
    )
    listings = ActiveListingsClient(oauth, session=session).get_active_listings()
    assert [item.sku for item in listings] == ["Q0001", "Q0002"]
    assert oauth.forces == [False, True, True]
    assert b"<PageNumber>2</PageNumber>" in session.calls[-1][1]["data"]


def test_sandbox_isolated_and_errors_are_sanitized():
    oauth = OAuth()
    oauth.config.environment = "sandbox"
    session = Session([Response(status=403)])
    with pytest.raises(ActiveListingsApiError, match="reconnect"):
        ActiveListingsClient(oauth, session=session).get_active_listings()
    assert session.calls[0][0] == "https://api.sandbox.ebay.com/ws/api.dll"


def failure_xml(*, secret="refresh-secret"):
    return f"""<?xml version="1.0"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack><Errors><ShortMessage>Token rejected.</ShortMessage>
      <LongMessage>Token was invalid; parameter {secret} was discarded.</LongMessage>
      <ErrorCode>931</ErrorCode><SeverityCode>Error</SeverityCode>
      <ErrorClassification>RequestError</ErrorClassification>
      <ErrorParameters><Value>{secret}</Value></ErrorParameters></Errors>
    </GetMyeBaySellingResponse>""".encode()


def test_http_auth_rejection_is_distinct_and_sanitized():
    session = Session([Response(status=401), Response(failure_xml(), status=401)])
    with pytest.raises(ActiveListingsApiError) as raised:
        ActiveListingsClient(OAuth(), session=session).get_active_listings()
    message = str(raised.value)
    assert "token or scope" in message and "HTTP 401" in message
    assert "ack=Failure" in message and "errorCode=931" in message
    assert "severity=Error" in message and "classification=RequestError" in message
    assert "refresh-secret" not in message
    assert "[REDACTED]" in message
    assert "ErrorParameters" not in message


def test_ack_failure_reports_allowlisted_diagnostics_without_raw_body():
    with pytest.raises(ActiveListingsApiError) as raised:
        ActiveListingsClient(
            OAuth(), session=Session([Response(failure_xml())])
        ).get_active_listings()
    message = str(raised.value)
    assert "Trading API rejected" in message
    for value in ("ack=Failure", "errorCode=931", "shortMessage=Token rejected."):
        assert value in message
    assert "refresh-secret" not in message
    assert "<Errors>" not in message and "ErrorParameters" not in message


def test_malformed_success_response_is_distinct():
    with pytest.raises(ActiveListingsApiError, match="malformed active-listing data"):
        ActiveListingsClient(OAuth(), session=Session([Response(b"not xml")])).get_active_listings()


def test_active_list_membership_supplies_status_when_optional_field_is_omitted():
    payload = xml().replace(b"<ListingStatus>Active</ListingStatus>", b"")
    listing = ActiveListingsClient(
        OAuth(), session=Session([Response(payload)])
    ).get_active_listings()[0]
    assert listing.status == "Active"
