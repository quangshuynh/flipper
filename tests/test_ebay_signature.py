import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from ebay.compliance import ACCOUNT_DELETION_PATH, app
from ebay.signature_verifier import (
    EbaySignatureVerifier,
    PublicKeyDocument,
    PublicKeyRetrievalError,
    SignatureFormatError,
)


KEY_ID = "test-key-id"
NOTIFICATION = {
    "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
    "notification": {"notificationId": "test-notification", "data": {}},
}
client = TestClient(app)


def public_key_document(private_key):
    """Return eBay-shaped public-key metadata for a test-only EC key."""
    pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return PublicKeyDocument(pem.decode(), "ECDSA", "SHA1")


def signature_header(notification, private_key, key_id=KEY_ID):
    """Create an eBay-shaped signature header using a test-only EC key."""
    payload = json.dumps(notification, separators=(",", ":"), ensure_ascii=False).encode()
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA1()))
    envelope = {
        "alg": "ecdsa",
        "kid": key_id,
        "signature": base64.b64encode(signature).decode(),
        "digest": "SHA1",
    }
    return base64.b64encode(json.dumps(envelope, separators=(",", ":")).encode()).decode()


@pytest.fixture
def private_key():
    """Create an ephemeral test-only key; no production key material is used."""
    return ec.generate_private_key(ec.SECP256R1())


def test_valid_signature(private_key):
    verifier = EbaySignatureVerifier(lambda _key_id: public_key_document(private_key))
    assert verifier.verify(NOTIFICATION, signature_header(NOTIFICATION, private_key)) is True


def test_invalid_signature(private_key):
    verifier = EbaySignatureVerifier(lambda _key_id: public_key_document(private_key))
    changed = {**NOTIFICATION, "unexpected": True}
    assert verifier.verify(changed, signature_header(NOTIFICATION, private_key)) is False


def test_malformed_signature_does_not_fetch_key():
    verifier = EbaySignatureVerifier(lambda _key_id: pytest.fail("must not fetch a key"))
    with pytest.raises(SignatureFormatError):
        verifier.verify(NOTIFICATION, "not-base64!")


def test_public_key_retrieval_failure(private_key):
    def fail(_key_id):
        raise PublicKeyRetrievalError("offline")

    verifier = EbaySignatureVerifier(fail)
    with pytest.raises(PublicKeyRetrievalError):
        verifier.verify(NOTIFICATION, signature_header(NOTIFICATION, private_key))


def test_public_key_cache_hit(private_key):
    calls = []

    def fetch(key_id):
        calls.append(key_id)
        return public_key_document(private_key)

    verifier = EbaySignatureVerifier(fetch)
    header = signature_header(NOTIFICATION, private_key)

    assert verifier.verify(NOTIFICATION, header) is True
    assert verifier.verify(NOTIFICATION, header) is True
    assert calls == [KEY_ID]


def test_public_key_cache_expiry_refetches(private_key):
    now = [100.0]
    calls = []

    def fetch(key_id):
        calls.append(key_id)
        return public_key_document(private_key)

    verifier = EbaySignatureVerifier(fetch, cache_ttl_seconds=3600, clock=lambda: now[0])
    header = signature_header(NOTIFICATION, private_key)

    assert verifier.verify(NOTIFICATION, header) is True
    now[0] += 3601
    assert verifier.verify(NOTIFICATION, header) is True
    assert calls == [KEY_ID, KEY_ID]


def test_post_rejects_missing_signature_without_processing(monkeypatch):
    processed = []
    monkeypatch.setattr("ebay.compliance.process_account_deletion", processed.append)
    response = client.post(ACCOUNT_DELETION_PATH, json=NOTIFICATION)
    assert response.status_code == 412
    assert processed == []


@pytest.mark.parametrize("verification_result", [False, SignatureFormatError("bad signature")])
def test_post_does_not_process_failed_verification(monkeypatch, verification_result):
    class StubVerifier:
        def verify(self, _notification, _signature):
            if isinstance(verification_result, Exception):
                raise verification_result
            return verification_result

    processed = []
    monkeypatch.setattr("ebay.compliance._get_signature_verifier", lambda: StubVerifier())
    monkeypatch.setattr("ebay.compliance.process_account_deletion", processed.append)
    response = client.post(
        ACCOUNT_DELETION_PATH, json=NOTIFICATION, headers={"X-EBAY-SIGNATURE": "invalid"}
    )
    assert response.status_code == 412
    assert processed == []


def test_post_reports_public_key_retrieval_failure(monkeypatch):
    class FailingVerifier:
        def verify(self, _notification, _signature):
            raise PublicKeyRetrievalError("offline")

    monkeypatch.setattr("ebay.compliance._get_signature_verifier", lambda: FailingVerifier())
    response = client.post(
        ACCOUNT_DELETION_PATH, json=NOTIFICATION, headers={"X-EBAY-SIGNATURE": "valid-shape"}
    )
    assert response.status_code == 500
