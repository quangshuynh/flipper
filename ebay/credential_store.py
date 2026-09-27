"""Owner-only file persistence for server-side eBay seller credentials.

This backend is for a single-user host whose persistent disk is already protected. It stores the
same service/username/secret values the OS keyring would hold, in plaintext JSON restricted to the
owning user. It adds no encryption because a key kept on the same host would not protect the
secret from anyone able to read the file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from keyring.errors import KeyringError, PasswordDeleteError

FORMAT_VERSION = 1
_POSIX = os.name == "posix"


class CredentialFileError(KeyringError):
    """The credential file could not be used. Messages never include credential values."""


def _parse(text: str) -> dict[str, dict[str, str]] | None:
    """Return validated credentials, or None when the stored document is malformed."""
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION:
        return None
    credentials = document.get("credentials")
    if not isinstance(credentials, dict):
        return None
    for service, entries in credentials.items():
        if not isinstance(service, str) or not isinstance(entries, dict):
            return None
        for username, secret in entries.items():
            if not isinstance(username, str) or not isinstance(secret, str):
                return None
    return credentials


class FileCredentialStore:
    """Implement the seller OAuth ``CredentialStore`` protocol with one JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"FileCredentialStore(path={str(self.path)!r})"

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            if _POSIX and os.stat(self.path).st_mode & 0o077:
                raise CredentialFileError(
                    "credential file permissions are too broad; restrict it to owner "
                    "read/write (chmod 600)"
                )
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            text = None
        # Raise outside the except/parse scope so no exception context retains file contents.
        if text is None:
            raise CredentialFileError("credential file could not be read")
        credentials = _parse(text)
        if credentials is None:
            raise CredentialFileError("credential file is malformed; reconnect to replace it")
        return credentials

    def _write(self, credentials: dict[str, dict[str, str]]) -> None:
        directory = self.path.parent
        staging = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            # mkstemp creates the file with owner-only permissions on POSIX.
            descriptor, staging = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=directory
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": FORMAT_VERSION, "credentials": credentials}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if _POSIX:
                os.chmod(staging, 0o600)
            os.replace(staging, self.path)
            staging = None
            if _POSIX:
                directory_descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except OSError:
            failed = True
        else:
            failed = False
        finally:
            if staging is not None:
                Path(staging).unlink(missing_ok=True)
        if failed:
            raise CredentialFileError("credential file could not be written")

    def get_password(self, service: str, username: str) -> str | None:
        with self._lock:
            return self._load().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        if not all(isinstance(value, str) for value in (service, username, password)):
            raise CredentialFileError("credential service, username, and value must be text")
        with self._lock:
            credentials = self._load()
            credentials.setdefault(service, {})[username] = password
            self._write(credentials)

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            credentials = self._load()
            entries = credentials.get(service, {})
            if username not in entries:
                raise PasswordDeleteError("credential not found")
            del entries[username]
            if not entries:
                del credentials[service]
            self._write(credentials)
