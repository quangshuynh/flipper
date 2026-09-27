import json
import os
import stat

import keyring
import pytest
from keyring.errors import KeyringError, PasswordDeleteError

import ebay.credential_store as credential_module
from ebay.credential_store import CredentialFileError, FileCredentialStore
from ebay.seller_oauth import (
    SellerOAuthClient,
    SellerOAuthConfig,
    SellerOAuthError,
    credential_store_from_environment,
)

POSIX = os.name == "posix"
SECRET = "refresh-token-secret-sentinel"
SERVICE = "flipper.ebay.seller.production"


@pytest.fixture(autouse=True)
def isolated_storage_environment(monkeypatch):
    for name in (
        "FLIPPER_DATA_DIR",
        "FLIPPER_INVENTORY_DB",
        "FLIPPER_ATTACHMENT_ROOT",
        "FLIPPER_CREDENTIAL_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_real_keyring(monkeypatch):
    def refuse(*_args):
        raise AssertionError("the real OS keyring must not be used")

    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, refuse)


def test_missing_file_reads_as_not_connected(tmp_path):
    store = FileCredentialStore(tmp_path / "credentials" / "ebay-seller.json")
    assert store.get_password(SERVICE, "client") is None
    assert not (tmp_path / "credentials").exists()


def test_set_get_overwrite_and_delete_round_trip(tmp_path):
    path = tmp_path / "credentials" / "ebay-seller.json"
    store = FileCredentialStore(path)

    store.set_password(SERVICE, "client", SECRET)
    assert store.get_password(SERVICE, "client") == SECRET
    assert FileCredentialStore(path).get_password(SERVICE, "client") == SECRET
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "credentials": {SERVICE: {"client": SECRET}},
    }

    store.set_password(SERVICE, "client", "replacement-token")
    store.set_password("flipper.ebay.seller.sandbox", "client", "sandbox-token")
    assert store.get_password(SERVICE, "client") == "replacement-token"
    assert store.get_password("flipper.ebay.seller.sandbox", "client") == "sandbox-token"

    store.delete_password(SERVICE, "client")
    assert store.get_password(SERVICE, "client") is None
    assert store.get_password("flipper.ebay.seller.sandbox", "client") == "sandbox-token"
    with pytest.raises(PasswordDeleteError):
        store.delete_password(SERVICE, "client")
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "contents",
    [
        f"not json {SECRET}",
        json.dumps({"version": 2, "credentials": {SERVICE: {"client": SECRET}}}),
        json.dumps({"version": 1, "credentials": {SERVICE: {"client": 7}}}),
        json.dumps([SECRET]),
    ],
)
def test_malformed_file_fails_safely_without_revealing_contents(tmp_path, contents):
    path = tmp_path / "ebay-seller.json"
    path.write_text(contents, encoding="utf-8")
    if POSIX:
        path.chmod(0o600)
    store = FileCredentialStore(path)

    with pytest.raises(CredentialFileError) as raised:
        store.get_password(SERVICE, "client")

    assert isinstance(raised.value, KeyringError)
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    with pytest.raises(CredentialFileError):
        store.set_password(SERVICE, "client", "new")
    assert path.read_text(encoding="utf-8") == contents


def test_failed_replacement_keeps_previous_file_and_removes_staging(monkeypatch, tmp_path):
    path = tmp_path / "ebay-seller.json"
    store = FileCredentialStore(path)
    store.set_password(SERVICE, "client", "original-token")
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(credential_module.os, "replace", fail_replace)
    with pytest.raises(CredentialFileError, match="could not be written") as raised:
        store.set_password(SERVICE, "client", SECRET)

    assert SECRET not in str(raised.value)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_repr_never_contains_credentials(tmp_path):
    store = FileCredentialStore(tmp_path / "ebay-seller.json")
    store.set_password(SERVICE, "client", SECRET)
    assert SECRET not in repr(store)
    assert SECRET not in str(vars(store))


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_posix_file_and_new_directory_are_owner_only(tmp_path):
    path = tmp_path / "data" / "credentials" / "ebay-seller.json"
    FileCredentialStore(path).set_password(SERVICE, "client", SECRET)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_posix_overwrite_restores_owner_only_mode(tmp_path):
    path = tmp_path / "ebay-seller.json"
    store = FileCredentialStore(path)
    store.set_password(SERVICE, "client", "first")
    store.set_password(SERVICE, "client", "second")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_posix_broadly_readable_file_is_refused(tmp_path):
    path = tmp_path / "ebay-seller.json"
    FileCredentialStore(path).set_password(SERVICE, "client", SECRET)
    path.chmod(0o644)

    with pytest.raises(CredentialFileError, match="chmod 600") as raised:
        FileCredentialStore(path).get_password(SERVICE, "client")
    assert SECRET not in str(raised.value)


def test_default_selection_is_the_os_keyring(monkeypatch):
    assert credential_store_from_environment() is keyring
    monkeypatch.setenv("FLIPPER_CREDENTIAL_BACKEND", "keyring")
    assert credential_store_from_environment() is keyring


def test_file_selection_uses_the_data_directory(monkeypatch, tmp_path, no_real_keyring):
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FLIPPER_CREDENTIAL_BACKEND", "file")

    store = credential_store_from_environment()

    assert isinstance(store, FileCredentialStore)
    assert store.path == tmp_path / "credentials" / "ebay-seller.json"
    assert not store.path.parent.exists()


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"FLIPPER_CREDENTIAL_BACKEND": "vault"}, "must be one of"),
        ({"FLIPPER_CREDENTIAL_BACKEND": "file"}, "requires FLIPPER_DATA_DIR"),
    ],
)
def test_invalid_selection_fails_as_safe_oauth_error(monkeypatch, environment, message):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SellerOAuthError, match=message):
        credential_store_from_environment()
    config = SellerOAuthConfig("production", "client", "secret", "runame")
    with pytest.raises(SellerOAuthError, match=message):
        SellerOAuthClient(config)
    assert config.connection_status().connected is None


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs["data"])
        return _Response(self.bodies.pop(0))


def test_seller_oauth_persists_and_reuses_refresh_token_in_file_backend(
    monkeypatch, tmp_path, no_real_keyring
):
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FLIPPER_CREDENTIAL_BACKEND", "file")
    config = SellerOAuthConfig("production", "client", "secret", "runame")
    assert config.connection_status().connected is False

    session = _Session(
        {"access_token": "access-1", "expires_in": 7200, "refresh_token": SECRET},
        {"access_token": "access-2", "expires_in": 7200},
    )
    SellerOAuthClient(config, session=session, clock=lambda: 0).exchange_code("code")

    assert config.connection_status().connected is True
    fresh = SellerOAuthClient(config, session=session, clock=lambda: 0)
    assert fresh.access_token() == "access-2"
    assert session.calls[-1]["refresh_token"] == SECRET
    assert fresh.disconnect() is True
    assert config.connection_status().connected is False
