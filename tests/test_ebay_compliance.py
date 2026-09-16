import hashlib

from fastapi.testclient import TestClient

from ebay.compliance import ACCOUNT_DELETION_PATH, app, create_challenge_response


TOKEN = "test_verification_token_32_chars_minimum"
ENDPOINT = "https://flipper.example.com/api/ebay/account-deletion"
client = TestClient(app)


def configure_endpoint(monkeypatch):
    """Configure non-secret test-only endpoint verification values."""
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_TOKEN", TOKEN)
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_ENDPOINT", ENDPOINT)


def test_known_challenge_response():
    expected = hashlib.sha256(f"challenge-123{TOKEN}{ENDPOINT}".encode()).hexdigest()

    assert create_challenge_response("challenge-123", TOKEN, ENDPOINT) == expected


def test_challenge_response_changes_when_each_input_changes():
    baseline = create_challenge_response("challenge", TOKEN, ENDPOINT)

    assert create_challenge_response("different", TOKEN, ENDPOINT) != baseline
    assert create_challenge_response("challenge", f"{TOKEN}x", ENDPOINT) != baseline
    assert create_challenge_response("challenge", TOKEN, f"{ENDPOINT}/") != baseline


def test_get_verification_success(monkeypatch):
    configure_endpoint(monkeypatch)

    response = client.get(ACCOUNT_DELETION_PATH, params={"challenge_code": "challenge-123"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "challengeResponse": create_challenge_response("challenge-123", TOKEN, ENDPOINT)
    }


def test_get_requires_challenge_code(monkeypatch):
    configure_endpoint(monkeypatch)

    response = client.get(ACCOUNT_DELETION_PATH)

    assert response.status_code == 422


def test_get_rejects_missing_server_configuration(monkeypatch):
    monkeypatch.delenv("EBAY_ACCOUNT_DELETION_TOKEN", raising=False)
    monkeypatch.delenv("EBAY_ACCOUNT_DELETION_ENDPOINT", raising=False)

    response = client.get(ACCOUNT_DELETION_PATH, params={"challenge_code": "challenge"})

    assert response.status_code == 503


def test_post_rejects_malformed_json():
    response = client.post(
        ACCOUNT_DELETION_PATH,
        content=b'{"notification":',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400


def test_post_acknowledges_valid_notification(monkeypatch):
    class AcceptingVerifier:
        def verify(self, _notification, _signature):
            return True

    processed = []
    monkeypatch.setattr("ebay.compliance._get_signature_verifier", lambda: AcceptingVerifier())
    monkeypatch.setattr("ebay.compliance.process_account_deletion", processed.append)
    notification = {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
        "notification": {"notificationId": "test-notification", "data": {}},
    }

    response = client.post(
        ACCOUNT_DELETION_PATH, json=notification, headers={"X-EBAY-SIGNATURE": "valid-shape"}
    )

    assert response.status_code == 204
    assert response.content == b""
    assert processed == [notification]
