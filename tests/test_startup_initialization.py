import sqlite3

import pytest
from fastapi.testclient import TestClient

import main as cli
import web.app as web_app
from inventory.store import CURRENT_SCHEMA_VERSION, InventoryStore
from storage_config import StorageConfigurationError
from web.app import app


@pytest.fixture(autouse=True)
def isolated_storage_environment(monkeypatch):
    for name in (
        "FLIPPER_DATA_DIR",
        "FLIPPER_INVENTORY_DB",
        "FLIPPER_ATTACHMENT_ROOT",
        "FLIPPER_CREDENTIAL_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_app, "_initialized_databases", set())
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)


def _versions(path):
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]


@pytest.fixture
def traced(monkeypatch):
    statements = []
    initializations = []
    original_connect = InventoryStore._connect
    original_initialize = InventoryStore.initialize

    def traced_connect(self):
        connection = original_connect(self)
        connection.set_trace_callback(statements.append)
        return connection

    def counted_initialize(self):
        initializations.append(self.path)
        return original_initialize(self)

    monkeypatch.setattr(InventoryStore, "_connect", traced_connect)
    monkeypatch.setattr(InventoryStore, "initialize", counted_initialize)
    return statements, initializations


def test_startup_migrates_before_any_request(monkeypatch, tmp_path):
    database = tmp_path / "startup.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))

    with TestClient(app):
        assert _versions(database) == list(range(1, CURRENT_SCHEMA_VERSION + 1))


def test_data_directory_selects_startup_database(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(tmp_path / "data"))

    with TestClient(app) as client:
        assert client.get("/inventory").status_code == 200

    assert _versions(tmp_path / "data" / "flipper_inventory.db")[-1] == CURRENT_SCHEMA_VERSION


def test_requests_after_startup_never_repeat_schema_initialization(monkeypatch, tmp_path, traced):
    statements, initializations = traced
    database = tmp_path / "requests.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    InventoryStore(database).add(
        title="Laptop",
        source="Local seller",
        acquired_at="2026-09-01",
        acquisition_cost="25.00",
    )
    initializations.clear()

    with TestClient(app) as client:
        assert len(initializations) == 1
        statements.clear()
        for path in ("/", "/inventory", "/inventory/Q0001", "/sales", "/insights", "/"):
            assert client.get(path).status_code == 200

    assert len(initializations) == 1
    assert statements, "requests should still read the database"
    assert not any("schema_migrations" in sql for sql in statements)
    assert not any(sql.strip().upper().startswith("BEGIN IMMEDIATE") for sql in statements)


def test_mutations_keep_their_own_transactions_after_startup(monkeypatch, tmp_path, traced):
    statements, initializations = traced
    database = tmp_path / "mutation.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    InventoryStore(database).add(
        title="Laptop",
        source="Local seller",
        acquired_at="2026-09-01",
        acquisition_cost="25.00",
    )
    initializations.clear()

    with TestClient(app) as client:
        statements.clear()
        response = client.post(
            "/inventory/Q0001/notes",
            data={"notes": "Tested"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert len(initializations) == 1
    assert not any("schema_migrations" in sql for sql in statements)
    assert any(sql.strip().upper().startswith("BEGIN IMMEDIATE") for sql in statements)
    assert InventoryStore(database).get("Q0001").notes == "Tested"


def test_first_request_without_lifespan_initializes_once(monkeypatch, tmp_path, traced):
    _, initializations = traced
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "lazy.db"))
    client = TestClient(app)

    for _ in range(3):
        assert client.get("/inventory").status_code == 200

    assert len(initializations) == 1


def test_invalid_storage_configuration_prevents_startup(monkeypatch):
    monkeypatch.setenv("FLIPPER_DATA_DIR", "relative/data")

    with pytest.raises(StorageConfigurationError):
        with TestClient(app):
            pass


def test_invalid_credential_backend_prevents_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "web.db"))
    monkeypatch.setenv("FLIPPER_CREDENTIAL_BACKEND", "vault")

    with pytest.raises(StorageConfigurationError):
        with TestClient(app):
            pass


def test_migration_failure_prevents_startup_and_is_not_cached(monkeypatch, tmp_path):
    database = tmp_path / "future.db"
    InventoryStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)", (CURRENT_SCHEMA_VERSION + 1,)
        )
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))

    with pytest.raises(RuntimeError, match="newer than this Flipper version"):
        with TestClient(app):
            pass

    assert web_app._initialized_databases == set()
    assert TestClient(app).get("/inventory").status_code == 503


def test_web_attachment_root_follows_shared_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "web.db"))
    store = web_app._store()
    assert web_app._attachments(store).root == tmp_path / "attachments"

    monkeypatch.setenv("FLIPPER_ATTACHMENT_ROOT", str(tmp_path / "files"))
    assert web_app._attachments(store).root == tmp_path / "files"


def test_cli_initializes_an_independent_database_without_the_web_app(tmp_path, capsys):
    database = tmp_path / "cli.db"

    assert cli.main(["inventory", "--database", str(database), "list"]) == 0

    assert _versions(database) == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert str(database) not in web_app._initialized_databases


def test_cli_default_database_is_unchanged_working_directory_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["reports", "summary"]) == 0

    assert _versions(tmp_path / "data" / "flipper_inventory.db")[-1] == CURRENT_SCHEMA_VERSION


def test_cli_uses_shared_configuration_and_explicit_flag_wins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(tmp_path / "volume"))
    assert cli.main(["inventory", "list"]) == 0
    assert (tmp_path / "volume" / "flipper_inventory.db").exists()

    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "configured.db"))
    assert cli.main(["sales", "list"]) == 0
    assert (tmp_path / "configured.db").exists()

    assert cli.main(["reports", "--database", str(tmp_path / "flag.db"), "summary"]) == 0
    assert (tmp_path / "flag.db").exists()
    assert not (tmp_path / "data").exists()


def test_cli_attachment_root_uses_configuration_unless_flag_given(monkeypatch, tmp_path, capsys):
    database = tmp_path / "cli.db"
    InventoryStore(database).add(
        title="Laptop",
        source="Local seller",
        acquired_at="2026-09-01",
        acquisition_cost="25.00",
    )
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    monkeypatch.setenv("FLIPPER_ATTACHMENT_ROOT", str(tmp_path / "configured-files"))

    base = ["inventory", "--database", str(database)]
    assert cli.main([*base, "attach", "Q0001", str(image)]) == 0
    assert len(list((tmp_path / "configured-files").iterdir())) == 1

    flag = [*base, "--attachment-root", str(tmp_path / "flag-files")]
    assert cli.main([*flag, "attach", "Q0001", str(image)]) == 0
    assert len(list((tmp_path / "flag-files").iterdir())) == 1


def test_cli_invalid_storage_configuration_fails_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLIPPER_DATA_DIR", "relative")

    assert cli.main(["inventory", "list"]) == 1

    assert "FLIPPER_DATA_DIR must be an absolute" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_schema_verified_store_requires_opt_in_reuse(tmp_path):
    with pytest.raises(ValueError, match="requires reuse_schema_check"):
        InventoryStore(tmp_path / "inventory.db", schema_verified=True)
    assert not (tmp_path / "inventory.db").exists()
