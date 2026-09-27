"""Browser-like test clients for Flipper's web security boundary."""

from fastapi.testclient import TestClient

LOCAL_ORIGIN = "http://localhost"


def local_client(app, **kwargs) -> TestClient:
    """A loopback client sending its own Origin, like a local browser using Flipper's forms."""
    headers = {"Origin": LOCAL_ORIGIN, **kwargs.pop("headers", {})}
    return TestClient(
        app, base_url=LOCAL_ORIGIN, client=("127.0.0.1", 50000), headers=headers, **kwargs
    )
