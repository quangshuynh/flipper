"""HTTP receiver for eBay marketplace account deletion notifications."""

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from ebay.signature_verifier import (
    EbaySignatureVerifier,
    PublicKeyRetrievalError,
    SignatureFormatError,
    build_signature_verifier,
)


ACCOUNT_DELETION_PATH = "/api/ebay/account-deletion"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,80}$")

app = FastAPI(
    title="Flipper eBay Compliance",
    description="Receives eBay marketplace account deletion/closure notifications.",
)
_signature_verifier: EbaySignatureVerifier | None = None


def create_challenge_response(challenge_code: str, verification_token: str, endpoint: str) -> str:
    """Return eBay's SHA-256 endpoint-verification response."""
    value = f"{challenge_code}{verification_token}{endpoint}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _get_verification_configuration() -> tuple[str, str]:
    """Read and validate the exact values used for eBay endpoint verification."""
    verification_token = os.getenv("EBAY_ACCOUNT_DELETION_TOKEN")
    endpoint = os.getenv("EBAY_ACCOUNT_DELETION_ENDPOINT")

    if not verification_token or not endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="eBay account deletion endpoint is not configured",
        )
    if not TOKEN_PATTERN.fullmatch(verification_token):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="eBay account deletion verification token is invalid",
        )

    parsed_endpoint = urlsplit(endpoint)
    if (
        parsed_endpoint.scheme != "https"
        or not parsed_endpoint.netloc
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="eBay account deletion endpoint URL is invalid",
        )

    return verification_token, endpoint


def process_account_deletion(_notification: dict[str, Any]) -> None:
    """Process a deletion request when Flipper begins persisting applicable user data.

    Flipper currently stores no eBay seller, buyer, or order records, so there is no
    applicable persisted user data to delete. Keep this boundary explicit so storage
    deletion can be implemented here before future seller-side persistence is enabled.
    """


def _get_signature_verifier() -> EbaySignatureVerifier:
    """Create one process-local verifier so its bounded public-key cache is reused."""
    global _signature_verifier
    if _signature_verifier is None:
        try:
            _signature_verifier = build_signature_verifier()
        except ValueError as exc:
            raise PublicKeyRetrievalError("eBay signature verification is not configured") from exc
    return _signature_verifier


@app.get(ACCOUNT_DELETION_PATH)
def verify_account_deletion_endpoint(
    challenge_code: str = Query(..., min_length=1),
) -> dict[str, str]:
    """Respond to eBay's endpoint ownership challenge."""
    verification_token, endpoint = _get_verification_configuration()
    return {
        "challengeResponse": create_challenge_response(challenge_code, verification_token, endpoint)
    }


@app.post(ACCOUNT_DELETION_PATH, status_code=status.HTTP_204_NO_CONTENT)
async def receive_account_deletion_notification(request: Request) -> Response:
    """Verify, process, and then acknowledge an eBay deletion notification."""
    raw_body = await request.body()
    try:
        notification = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON",
        ) from exc

    if not isinstance(notification, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Notification must be a JSON object",
        )

    signature_header = request.headers.get("X-EBAY-SIGNATURE")
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="X-EBAY-SIGNATURE header is required",
        )
    try:
        verified = await run_in_threadpool(
            _get_signature_verifier().verify, notification, signature_header
        )
    except SignatureFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="eBay notification signature is invalid",
        ) from exc
    except PublicKeyRetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="eBay notification signature verification is unavailable",
        ) from exc
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="eBay notification signature is invalid",
        )

    process_account_deletion(notification)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
