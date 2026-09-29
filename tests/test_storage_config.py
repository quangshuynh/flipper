from pathlib import Path

import pytest

from storage_config import StorageConfigurationError, resolve_storage


def test_unconfigured_defaults_preserve_historical_locations(tmp_path):
    cli = resolve_storage({})
    assert cli.inventory_database == Path("data") / "flipper_inventory.db"
    assert cli.attachment_root is None
    assert cli.data_dir is None
    assert cli.credential_backend == "keyring"
    assert cli.credential_file is None

    web = resolve_storage({}, default_root=tmp_path)
    assert web.inventory_database == tmp_path / "data" / "flipper_inventory.db"


def test_blank_values_are_treated_as_unset(tmp_path):
    config = resolve_storage(
        {
            "FLIPPER_DATA_DIR": "  ",
            "FLIPPER_INVENTORY_DB": "",
            "FLIPPER_ATTACHMENT_ROOT": "",
            "FLIPPER_CREDENTIAL_BACKEND": "",
        },
        default_root=tmp_path,
    )
    assert config == resolve_storage({}, default_root=tmp_path)


def test_data_directory_supplies_database_and_sibling_attachment_defaults(tmp_path):
    config = resolve_storage({"FLIPPER_DATA_DIR": str(tmp_path / "data")})
    assert config.data_dir == tmp_path / "data"
    assert config.inventory_database == tmp_path / "data" / "flipper_inventory.db"
    # None keeps AttachmentService's established "attachments beside the database" rule.
    assert config.attachment_root is None


def test_explicit_database_and_attachment_paths_take_precedence(tmp_path):
    config = resolve_storage(
        {
            "FLIPPER_DATA_DIR": str(tmp_path / "data"),
            "FLIPPER_INVENTORY_DB": str(tmp_path / "other" / "inventory.db"),
            "FLIPPER_ATTACHMENT_ROOT": str(tmp_path / "files"),
        },
        default_root=tmp_path / "repo",
    )
    assert config.inventory_database == tmp_path / "other" / "inventory.db"
    assert config.attachment_root == tmp_path / "files"


def test_existing_relative_database_path_remains_supported():
    config = resolve_storage({"FLIPPER_INVENTORY_DB": "local/test.db"})
    assert config.inventory_database == Path("local/test.db")


def test_relative_data_directory_is_rejected():
    with pytest.raises(StorageConfigurationError, match="FLIPPER_DATA_DIR must be an absolute"):
        resolve_storage({"FLIPPER_DATA_DIR": "relative/data"})


def test_file_credential_backend_uses_the_data_directory(tmp_path):
    config = resolve_storage(
        {"FLIPPER_DATA_DIR": str(tmp_path), "FLIPPER_CREDENTIAL_BACKEND": " File "}
    )
    assert config.credential_backend == "file"
    assert config.credential_file == tmp_path / "credentials" / "ebay-seller.json"


def test_file_credential_backend_requires_the_data_directory(tmp_path):
    with pytest.raises(StorageConfigurationError, match="requires FLIPPER_DATA_DIR"):
        resolve_storage(
            {
                "FLIPPER_CREDENTIAL_BACKEND": "file",
                "FLIPPER_INVENTORY_DB": str(tmp_path / "inventory.db"),
            }
        )


def test_invalid_credential_backend_fails_clearly():
    with pytest.raises(StorageConfigurationError, match="keyring, file"):
        resolve_storage({"FLIPPER_CREDENTIAL_BACKEND": "vault"})


def test_resolution_never_touches_the_filesystem(tmp_path):
    data_dir = tmp_path / "not-created"
    resolve_storage({"FLIPPER_DATA_DIR": str(data_dir), "FLIPPER_CREDENTIAL_BACKEND": "file"})
    assert not data_dir.exists()
    assert list(tmp_path.iterdir()) == []
