"""Resolve where Flipper keeps writable authoritative data and server-side credentials.

Resolution is pure: it reads a mapping of environment values and never creates, moves, or
copies files. Callers create directories only when they actually write.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV = "FLIPPER_DATA_DIR"
INVENTORY_DB_ENV = "FLIPPER_INVENTORY_DB"
ATTACHMENT_ROOT_ENV = "FLIPPER_ATTACHMENT_ROOT"
CREDENTIAL_BACKEND_ENV = "FLIPPER_CREDENTIAL_BACKEND"

INVENTORY_DB_FILENAME = "flipper_inventory.db"
CREDENTIALS_DIRNAME = "credentials"
SELLER_CREDENTIAL_FILENAME = "ebay-seller.json"

KEYRING_BACKEND = "keyring"
FILE_BACKEND = "file"
CREDENTIAL_BACKENDS = (KEYRING_BACKEND, FILE_BACKEND)


class StorageConfigurationError(ValueError):
    """Storage configuration is invalid; the message names only settings, never secrets."""


@dataclass(frozen=True)
class StorageConfig:
    """Resolved writable locations.

    ``attachment_root`` is None when attachments use their established default: an
    ``attachments`` directory beside the selected inventory database.
    """

    inventory_database: Path
    attachment_root: Path | None
    data_dir: Path | None
    credential_backend: str
    credential_file: Path | None


def _value(environ: Mapping[str, str], name: str) -> str | None:
    """Treat unset and blank values alike, matching `.env.example` guidance."""
    value = environ.get(name, "").strip()
    return value or None


def resolve_storage(
    environ: Mapping[str, str] | None = None, *, default_root: str | Path = ""
) -> StorageConfig:
    """Resolve storage locations with explicit, documented precedence.

    Inventory database: ``FLIPPER_INVENTORY_DB``, else ``FLIPPER_DATA_DIR/flipper_inventory.db``,
    else ``<default_root>/data/flipper_inventory.db``. The web app passes the repository root;
    the CLI passes an empty root so its historical working-directory-relative default is
    unchanged. Command-line ``--database`` / ``--attachment-root`` flags override these values
    in the CLI.

    Attachments: ``FLIPPER_ATTACHMENT_ROOT``, else beside the selected inventory database.

    Credentials: ``FLIPPER_CREDENTIAL_BACKEND`` selects ``keyring`` (default) or ``file``. The file
    backend stores ``FLIPPER_DATA_DIR/credentials/ebay-seller.json`` and requires the data
    directory so a secret is never written to an implicit repository-relative location.
    """
    environ = os.environ if environ is None else environ
    data_dir_value = _value(environ, DATA_DIR_ENV)
    data_dir = None
    if data_dir_value is not None:
        data_dir = Path(data_dir_value)
        if not data_dir.is_absolute():
            raise StorageConfigurationError(f"{DATA_DIR_ENV} must be an absolute directory path")

    database_value = _value(environ, INVENTORY_DB_ENV)
    if database_value is not None:
        inventory_database = Path(database_value)
    elif data_dir is not None:
        inventory_database = data_dir / INVENTORY_DB_FILENAME
    else:
        inventory_database = Path(default_root) / "data" / INVENTORY_DB_FILENAME

    attachment_value = _value(environ, ATTACHMENT_ROOT_ENV)
    attachment_root = Path(attachment_value) if attachment_value is not None else None

    backend = (_value(environ, CREDENTIAL_BACKEND_ENV) or KEYRING_BACKEND).lower()
    if backend not in CREDENTIAL_BACKENDS:
        raise StorageConfigurationError(
            f"{CREDENTIAL_BACKEND_ENV} must be one of: {', '.join(CREDENTIAL_BACKENDS)}"
        )
    credential_file = None
    if backend == FILE_BACKEND:
        if data_dir is None:
            raise StorageConfigurationError(
                f"{CREDENTIAL_BACKEND_ENV}=file requires {DATA_DIR_ENV} to be set"
            )
        credential_file = data_dir / CREDENTIALS_DIRNAME / SELLER_CREDENTIAL_FILENAME

    return StorageConfig(
        inventory_database=inventory_database,
        attachment_root=attachment_root,
        data_dir=data_dir,
        credential_backend=backend,
        credential_file=credential_file,
    )
