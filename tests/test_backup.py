import hashlib
import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import backup.service as service
import main as cli
from backup.fingerprint import database_state
from backup.service import (
    BackupError,
    BackupStatus,
    RestoreError,
    create_backup,
    restore_backup,
    verify_backup,
    verify_restored,
)
from inventory.attachments import AttachmentService
from inventory.store import CURRENT_SCHEMA_VERSION, InventoryStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff" + b"synthetic-jpeg-bytes" * 50
PDF = b"%PDF-1.7\nsynthetic receipt\n%%EOF"
SECRET = "refresh-token-DO-NOT-COPY"


@pytest.fixture(autouse=True)
def isolated_storage_environment(monkeypatch):
    for name in (
        "FLIPPER_DATA_DIR",
        "FLIPPER_INVENTORY_DB",
        "FLIPPER_ATTACHMENT_ROOT",
        "FLIPPER_CREDENTIAL_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)


def payload(title="Camera"):
    return {
        "schema_version": 1,
        "opportunity": {"title": title},
        "source_facts": {},
        "assumptions": {},
        "comparables": [],
        "derived": {},
    }


def populate(data_dir: Path) -> InventoryStore:
    """Synthetic authoritative data covering every durable domain table."""
    store = InventoryStore(data_dir / "flipper_inventory.db", clock=lambda: NOW)
    store.add(
        title="Known zero",
        source="curb",
        acquired_at="2026-09-01",
        acquisition_cost="0.00",
        notes="free; cost is a known zero",
    )
    store.add(
        title="Recorder",
        source="thrift",
        acquired_at="2026-09-02",
        acquisition_cost="12.34",
        marketplace="ebay",
        marketplace_item_id="137744631273",
        marketplace_sku="Q0002",
    )
    store.transition_status("Q0002", "listed")
    store.adopt_ebay_listing(
        "Q0005",
        title="Unknown history",
        source=None,
        acquired_at=None,
        acquisition_cost=None,
        marketplace_item_id="item-5",
        marketplace_sku="Q0005",
    )
    sale, _ = store.import_sale(
        inventory_id="Q0002",
        marketplace="ebay",
        external_order_id="12-34567-89012",
        external_line_item_id="line-1",
        marketplace_sku="Q0002",
        quantity=1,
        gross_amount=Decimal("83.07"),
        currency="USD",
        sold_at=datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc),
        item_revenue=Decimal("75.00"),
        buyer_shipping=Decimal("8.07"),
        marketplace_tax=Decimal("3.98"),
    )
    store.add_sale_cost(
        sale.sale_id, category="marketplace_fee", amount=Decimal("12.24"), note="final value"
    )
    store.import_ebay_finances_cost(
        sale.sale_id,
        category="shipping_cost",
        amount=Decimal("5.100"),
        currency="USD",
        external_transaction_id="txn-1",
        external_component_key="label",
    )
    store.set_reconciliation_confirmation(sale.sale_id, "fees", confirmed=True)
    store.add_valuation_snapshot(
        "Q0001",
        analyzed_at=NOW,
        currency="USD",
        estimated_market_value=Decimal("50"),
        expected_resale_value=Decimal("45.00"),
        asking_price=Decimal("0"),
        ideal_buy_price=Decimal("20"),
        estimated_gross_profit=Decimal("45"),
        estimated_roi=None,
        deal_score=7,
    )
    snapshot = store.save_research_snapshot(
        opportunity_identity="manual:camera",
        source="estate-sale",
        title="Camera",
        category="electronics",
        currency="USD",
        asking_price=Decimal("20.250"),
        expected_resale=Decimal("80"),
        expected_profit=None,
        payload=payload(),
        save_token="a" * 32,
    )
    store.link_research_snapshot(snapshot.snapshot_id, "Q0001")
    store.set_sourcing_travel("Q0001", round_trip_miles="12.5", fuel_cost="0", travel_minutes=30)
    service_ = AttachmentService(store)
    (data_dir.parent / "photo.jpg").write_bytes(JPEG)
    (data_dir.parent / "receipt.pdf").write_bytes(PDF)
    service_.add("Q0002", data_dir.parent / "photo.jpg", category="product_photo")
    service_.add("Q0001", data_dir.parent / "receipt.pdf", category="receipt")
    return store


def tree_digest(root: Path) -> dict[str, tuple[str, int]]:
    """Content hash and mtime of every file below root, for no-mutation assertions."""
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def backup_files(backup: Path) -> set[str]:
    return {path.relative_to(backup).as_posix() for path in backup.rglob("*")}


def table_rows(database: Path) -> dict[str, list]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: [
                tuple((type(value).__name__, value) for value in row)
                for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            for table in tables
        }
    finally:
        connection.close()


def reseal(backup: Path, **overrides) -> None:
    """Rewrite manifest facts from the backup's current contents, as a careful forger would."""
    manifest = json.loads((backup / "manifest.json").read_text())
    database = backup / "flipper_inventory.db"
    state = database_state(database, immutable=True)
    manifest["database"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest["database"]["size_bytes"] = database.stat().st_size
    manifest["logical_fingerprint"]["sha256"] = state.fingerprint
    manifest["table_row_counts"] = state.table_row_counts
    manifest["counters"] = state.counters
    manifest["schema_migrations"] = state.schema_versions
    manifest["flipper_schema_version"] = state.schema_version
    manifest.update(overrides)
    write_manifest(backup, manifest, seal=True)


def write_manifest(backup: Path, manifest: dict, *, seal: bool) -> None:
    if seal:
        manifest = service._seal(manifest)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))


@pytest.fixture
def source(tmp_path):
    store = populate(tmp_path / "data")
    return SimpleNamespace(
        store=store,
        database=store.path,
        data_dir=tmp_path / "data",
        attachments=tmp_path / "data" / "attachments",
    )


@pytest.fixture
def made(source, tmp_path):
    result = create_backup(source.database, tmp_path / "backups", name="b1", clock=lambda: NOW)
    return result.path


# Backup creation


def test_populated_backup_is_valid_complete_and_exact(source, tmp_path):
    result = create_backup(source.database, tmp_path / "backups", clock=lambda: NOW)

    assert result.path == tmp_path / "backups" / "flipper-backup-20260920-120000"
    assert verify_backup(result.path).status is BackupStatus.VALID
    manifest = result.manifest
    assert manifest["format"] == "flipper-backup"
    assert manifest["format_version"] == 1
    assert manifest["created_at"] == "2026-09-20T12:00:00Z"
    assert manifest["flipper_schema_version"] == CURRENT_SCHEMA_VERSION
    assert manifest["schema_migrations"] == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert manifest["counters"] == {"q_number": 5, "s_number": 1, "c_number": 2}
    assert manifest["credentials_included"] is False
    counts = manifest["table_row_counts"]
    assert counts["inventory_items"] == 3
    assert counts["sales"] == 1
    assert counts["sale_costs"] == 2
    assert counts["sale_reconciliation_confirmations"] == 1
    assert counts["valuation_snapshots"] == 1
    assert counts["research_snapshots"] == 1
    assert counts["sourcing_travel"] == 1
    assert counts["inventory_attachments"] == 2
    assert manifest["logical_fingerprint"]["sha256"] == database_state(source.database).fingerprint
    # Every stored value, including NULL-vs-zero, scaled money, timestamps, JSON text, external
    # identities, and confirmations, is carried over exactly.
    assert table_rows(result.path / "flipper_inventory.db") == table_rows(source.database)
    attachments = manifest["attachments"]
    assert attachments["count"] == 2
    assert attachments["total_bytes"] == len(JPEG) + len(PDF)
    for entry in attachments["files"]:
        copied = (result.path / entry["path"]).read_bytes()
        assert copied == (source.attachments / Path(entry["path"]).name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(copied).hexdigest()
    # No absolute or source-location path leaks into the manifest.
    assert not any(tmp_path.name in value or ":\\" in value for value in strings(manifest))


def test_backup_preserves_unknown_known_zero_and_exact_money(made):
    rows = table_rows(made / "flipper_inventory.db")
    columns = [
        row[1]
        for row in sqlite3.connect(made / "flipper_inventory.db").execute(
            "PRAGMA table_info(inventory_items)"
        )
    ]
    items = {row[columns.index("inventory_id")][1]: row for row in rows["inventory_items"]}
    cost = columns.index("acquisition_cost_cents")
    assert items["Q0001"][cost] == ("int", 0)
    assert items["Q0005"][cost] == ("NoneType", None)
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        assert connection.execute(
            "SELECT gross_amount_minor, gross_amount_scale, checkout_total_minor FROM sales"
        ).fetchone() == (8307, 2, None)
        assert connection.execute(
            "SELECT amount_minor, amount_scale FROM sale_costs WHERE source = 'ebay_finances'"
        ).fetchone() == (51, 1)


def test_empty_database_backup_has_no_attachments(tmp_path):
    database = tmp_path / "data" / "flipper_inventory.db"
    InventoryStore(database).initialize()

    result = create_backup(database, tmp_path / "out", name="empty")

    assert verify_backup(result.path).ok
    assert result.manifest["counters"] == {"q_number": 0, "s_number": 0, "c_number": 0}
    assert result.manifest["attachments"]["count"] == 0
    assert backup_files(result.path) == {"manifest.json", "flipper_inventory.db", "attachments"}
    assert set(result.manifest["table_row_counts"].values()) <= {0, CURRENT_SCHEMA_VERSION, 1}


def test_backup_leaves_source_database_and_attachments_unchanged(source, tmp_path):
    before = tree_digest(source.data_dir)

    create_backup(source.database, tmp_path / "out")

    assert tree_digest(source.data_dir) == before
    assert not list(source.data_dir.glob("*-journal"))
    # The source remains usable afterwards.
    assert source.store.get("Q0001").title == "Known zero"


def test_backup_excludes_uncommitted_concurrent_write(source, tmp_path):
    writer = sqlite3.connect(source.database, timeout=30)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE inventory_id_sequence SET last_value = 99")
    writer.execute("UPDATE inventory_items SET notes = 'uncommitted' WHERE inventory_id = 'Q0001'")
    try:
        result = create_backup(source.database, tmp_path / "out", name="during")
    finally:
        writer.commit()
        writer.close()

    assert verify_backup(result.path).ok
    assert result.manifest["counters"]["q_number"] == 5
    with sqlite3.connect(result.path / "flipper_inventory.db") as connection:
        notes = connection.execute(
            "SELECT notes FROM inventory_items WHERE inventory_id = 'Q0001'"
        ).fetchone()[0]
    assert notes == "free; cost is a known zero"
    assert source.store.get("Q0001").notes == "uncommitted"


def test_backup_during_continuous_writes_is_one_consistent_state(source, tmp_path):
    stop = threading.Event()
    errors = []

    def write():
        store = InventoryStore(source.database, reuse_schema_check=True)
        count = 0
        try:
            while not stop.is_set() and count < 400:
                store.add(
                    title=f"Concurrent {count}",
                    source="load",
                    acquired_at="2026-09-03",
                    acquisition_cost="1.00",
                )
                count += 1
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    try:
        results = [
            create_backup(source.database, tmp_path / "out", name=f"b{index}") for index in range(3)
        ]
    finally:
        stop.set()
        thread.join()

    assert errors == []
    for result in results:
        assert verify_backup(result.path).ok
        with sqlite3.connect(result.path / "flipper_inventory.db") as connection:
            last = connection.execute("SELECT last_value FROM inventory_id_sequence").fetchone()[0]
            highest = connection.execute(
                "SELECT MAX(CAST(substr(inventory_id, 2) AS INTEGER)) FROM inventory_items"
            ).fetchone()[0]
            count = connection.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
        # The allocator and the rows it allocated were captured from the same commit.
        assert last == highest
        assert result.manifest["table_row_counts"]["inventory_items"] == count


def test_existing_backup_destination_is_never_overwritten(made, source, tmp_path):
    before = tree_digest(made)

    with pytest.raises(BackupError, match="already exists"):
        create_backup(source.database, tmp_path / "backups", name="b1")

    assert tree_digest(made) == before
    assert [path.name for path in (tmp_path / "backups").iterdir()] == ["b1"]


def test_failed_backup_leaves_no_artifact(source, tmp_path, monkeypatch):
    calls = []
    original = service._copy_file

    def failing_copy(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(src, dst)

    monkeypatch.setattr(service, "_copy_file", failing_copy)
    with pytest.raises(OSError, match="disk full"):
        create_backup(source.database, tmp_path / "out", name="broken")

    assert list((tmp_path / "out").iterdir()) == []


def test_failed_final_verification_leaves_no_artifact(source, tmp_path, monkeypatch):
    monkeypatch.setattr(
        service,
        "verify_backup",
        lambda path: service.VerificationResult(BackupStatus.INVALID, ("simulated",)),
    )
    with pytest.raises(BackupError, match="simulated"):
        create_backup(source.database, tmp_path / "out", name="unverified")
    assert list((tmp_path / "out").iterdir()) == []


def test_missing_referenced_attachment_fails_instead_of_claiming_completeness(
    source, tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "time", SimpleNamespace(sleep=lambda seconds: None))
    victim = sorted(source.attachments.iterdir())[0]
    victim.unlink()

    with pytest.raises(BackupError, match="missing from the attachment directory"):
        create_backup(source.database, tmp_path / "out", name="incomplete")

    assert list((tmp_path / "out").iterdir()) == []


def test_attachment_removal_in_flight_is_retried(source, tmp_path, monkeypatch):
    victim = sorted(source.attachments.iterdir())[0]
    parked = victim.with_name(".parked")
    victim.replace(parked)
    monkeypatch.setattr(
        service, "time", SimpleNamespace(sleep=lambda seconds: parked.replace(victim))
    )

    result = create_backup(source.database, tmp_path / "out", name="retried")

    assert result.manifest["source"]["capture_attempts"] == 2
    assert result.manifest["attachments"]["count"] == 2
    assert verify_backup(result.path).ok


def test_attachment_size_disagreeing_with_metadata_is_refused(source, tmp_path):
    victim = sorted(source.attachments.iterdir())[0]
    victim.write_bytes(victim.read_bytes()[:-1])

    with pytest.raises(BackupError, match="refusing to back up inconsistent data"):
        create_backup(source.database, tmp_path / "out", name="mismatch")
    assert list((tmp_path / "out").iterdir()) == []


def test_unreferenced_attachment_files_are_reported_excluded_and_untouched(source, tmp_path):
    (source.attachments / "stray.jpg").write_bytes(JPEG)
    (source.attachments / ".abc.tmp").write_bytes(b"partial")

    result = create_backup(source.database, tmp_path / "out", name="orphans")

    assert result.unreferenced_files == (".abc.tmp", "stray.jpg")
    assert result.manifest["source"]["unreferenced_attachment_files_excluded"] == 2
    assert not (result.path / "attachments" / "stray.jpg").exists()
    assert (source.attachments / "stray.jpg").exists()
    assert verify_backup(result.path).ok


@pytest.mark.parametrize("inside", ["data", "data/attachments", "data/attachments/deeper"])
def test_backup_output_inside_source_tree_is_refused(source, tmp_path, inside):
    with pytest.raises(BackupError, match="must be outside"):
        create_backup(source.database, tmp_path / inside)
    assert not (tmp_path / "data" / "attachments" / "deeper").exists()


def test_missing_source_is_not_created(tmp_path):
    with pytest.raises(BackupError, match="does not exist"):
        create_backup(tmp_path / "data" / "absent.db", tmp_path / "out")
    assert not (tmp_path / "data").exists()


def test_non_flipper_database_is_refused(tmp_path):
    database = tmp_path / "data" / "other.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(BackupError, match="not a Flipper database"):
        create_backup(database, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_newer_schema_source_is_refused_and_not_modified(source, tmp_path):
    with sqlite3.connect(source.database) as connection:
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (99,))
    before = tree_digest(source.data_dir)

    with pytest.raises(BackupError, match="newer"):
        create_backup(source.database, tmp_path / "out")

    assert tree_digest(source.data_dir) == before


def test_unsafe_backup_names_are_refused(source, tmp_path):
    for name in ("../escape", ".hidden", "a/b", ""):
        with pytest.raises(BackupError):
            create_backup(source.database, tmp_path / "out", name=name or "..")


# Credentials boundary


def test_credentials_env_and_unrelated_data_dir_files_are_never_captured(tmp_path, monkeypatch):
    data_dir = tmp_path / "srv" / "data"
    populate(data_dir)
    credentials = data_dir / "credentials"
    credentials.mkdir()
    (credentials / "ebay-seller.json").write_text(json.dumps({"refresh_token": SECRET}))
    (data_dir / ".env").write_text(f"EBAY_SELLER_CLIENT_SECRET={SECRET}\n")
    (data_dir / "notes.txt").write_text(SECRET)
    (data_dir / "flipper_seen.db").write_bytes(b"cache")
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FLIPPER_CREDENTIAL_BACKEND", "file")

    assert cli.main(["backup", "create", "--output-dir", str(tmp_path / "out"), "--name", "b"]) == 0

    backup = tmp_path / "out" / "b"
    files = backup_files(backup)
    assert files - {"manifest.json", "flipper_inventory.db", "attachments"} == {
        entry["path"]
        for entry in json.loads((backup / "manifest.json").read_text())["attachments"]["files"]
    }
    for path in backup.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
    manifest_text = (backup / "manifest.json").read_text()
    assert "ebay-seller.json" not in manifest_text
    assert '.env"' not in manifest_text


def test_attachment_root_misconfigured_as_data_dir_still_excludes_credentials(tmp_path):
    data_dir = tmp_path / "data"
    store = InventoryStore(data_dir / "flipper_inventory.db")
    store.add(title="Item", source="s", acquired_at="2026-09-01", acquisition_cost="1.00")
    (tmp_path / "photo.jpg").write_bytes(JPEG)
    AttachmentService(store, data_dir).add("Q0001", tmp_path / "photo.jpg")
    (data_dir / "credentials").mkdir()
    (data_dir / "credentials" / "ebay-seller.json").write_text(SECRET)
    (data_dir / ".env").write_text(SECRET)

    result = create_backup(store.path, tmp_path / "out", attachment_root=data_dir, name="b")

    assert len(list((result.path / "attachments").iterdir())) == 1
    assert {"credentials", ".env", "flipper_inventory.db"} <= set(result.unreferenced_files)
    for path in result.path.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


# Verification


def test_verification_does_not_modify_backup(made):
    before = tree_digest(made)
    assert verify_backup(made).ok
    assert tree_digest(made) == before
    assert backup_files(made) == set(tree_digest(made)) | {"attachments"}


def test_modified_database_fails_hash_verification(made):
    database = made / "flipper_inventory.db"
    data = bytearray(database.read_bytes())
    data[-10] ^= 0xFF
    database.write_bytes(bytes(data))

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert "database sha256 does not match the manifest" in result.problems


def test_truncated_database_fails(made):
    database = made / "flipper_inventory.db"
    database.write_bytes(database.read_bytes()[:4096])

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert "database size does not match the manifest" in result.problems


def test_invalid_sqlite_fails_even_with_resealed_hash(made):
    database = made / "flipper_inventory.db"
    database.write_bytes(b"not a database" * 100)
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["database"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest["database"]["size_bytes"] = database.stat().st_size
    write_manifest(made, manifest, seal=True)

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert any("SQLite" in problem or "open" in problem for problem in result.problems)


def test_foreign_key_inconsistency_fails(made):
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        connection.execute(
            "INSERT INTO sale_reconciliation_confirmations VALUES (999, 'fees', '2026')"
        )
    reseal(made)

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert result.problems == ("SQLite foreign_key_check reported 1 violation(s)",)


def test_modified_manifest_fails_checksum(made):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["created_at"] = "2020-01-01T00:00:00Z"
    write_manifest(made, manifest, seal=False)

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert "manifest checksum mismatch" in result.problems[0]


def test_wrong_row_count_fails(made):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["table_row_counts"]["inventory_items"] = 99
    write_manifest(made, manifest, seal=True)

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert "table inventory_items has 3 rows; manifest records 99" in result.problems


def test_wrong_counter_fails(made):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["counters"]["q_number"] = 4
    write_manifest(made, manifest, seal=True)

    assert "Q/S/C counter values do not match the manifest" in verify_backup(made).problems


def test_substituted_logical_state_fails_fingerprint(made):
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        connection.execute("UPDATE inventory_items SET title = 'Changed' WHERE internal_id = 1")
    manifest = json.loads((made / "manifest.json").read_text())
    database = made / "flipper_inventory.db"
    manifest["database"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest["database"]["size_bytes"] = database.stat().st_size
    write_manifest(made, manifest, seal=True)

    result = verify_backup(made)

    assert result.problems == ("logical fingerprint does not match the manifest",)


@pytest.mark.parametrize("seal", [True, False])
def test_unsupported_future_format_fails_safely(made, seal):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["format_version"] = 2
    write_manifest(made, manifest, seal=seal)

    result = verify_backup(made)

    assert result.status is BackupStatus.UNSUPPORTED_FORMAT
    assert "format version 2 is not supported" in result.problems[0]


def test_newer_schema_backup_fails_safely(made):
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (99,))
    reseal(made)

    result = verify_backup(made)

    assert result.status is BackupStatus.NEWER_SCHEMA
    assert "v99" in result.problems[0]


def test_database_schema_disagreeing_with_manifest_is_invalid(made):
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (99,))
    reseal(made, flipper_schema_version=CURRENT_SCHEMA_VERSION)
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["schema_migrations"] = list(range(1, CURRENT_SCHEMA_VERSION + 1))
    write_manifest(made, manifest, seal=True)

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert any("does not match manifest" in problem for problem in result.problems)


def test_missing_attachment_fails(made):
    victim = sorted((made / "attachments").iterdir())[0]
    victim.unlink()

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert any(problem.endswith("file is missing") for problem in result.problems)


def test_modified_attachment_fails(made):
    victim = sorted((made / "attachments").iterdir())[0]
    data = bytearray(victim.read_bytes())
    data[-1] ^= 0xFF
    victim.write_bytes(bytes(data))

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert any(problem.endswith("sha256 does not match") for problem in result.problems)


@pytest.mark.parametrize("location", ["notes.txt", "attachments/extra.jpg"])
def test_unexpected_files_make_backup_invalid(made, location):
    (made / location).write_bytes(b"extra")

    result = verify_backup(made)

    assert result.status is BackupStatus.INVALID
    assert any(Path(location).name in problem for problem in result.problems)


def test_missing_manifest_or_directory_is_not_a_backup(made, tmp_path):
    (made / "manifest.json").unlink()
    assert "manifest.json is missing" in verify_backup(made).problems[0]
    assert verify_backup(tmp_path / "nowhere").status is BackupStatus.INVALID


def test_malformed_manifest_values_are_rejected(made):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["attachments"]["files"][0]["path"] = "attachments/../../escape.jpg"
    write_manifest(made, manifest, seal=True)

    result = verify_backup(made)

    assert result.problems == ("manifest is malformed: attachment path is unsafe",)


def test_verification_works_after_backup_is_moved(made, tmp_path):
    moved = tmp_path / "elsewhere" / "copy"
    shutil.copytree(made, moved)
    assert verify_backup(moved).ok


# Restore


def test_restore_creates_equivalent_new_destination(made, source, tmp_path):
    backup_before = tree_digest(made)
    destination = tmp_path / "restored" / "data"

    result = restore_backup(made, destination, active_database=source.database)

    assert result.destination == destination
    assert sorted(path.name for path in destination.iterdir()) == [
        "attachments",
        "flipper_inventory.db",
    ]
    restored_state = database_state(destination / "flipper_inventory.db")
    assert restored_state.fingerprint == database_state(source.database).fingerprint
    assert restored_state.fingerprint == result.manifest["logical_fingerprint"]["sha256"]
    for original in source.attachments.iterdir():
        assert (destination / "attachments" / original.name).read_bytes() == original.read_bytes()
    assert verify_restored(made, destination).ok
    assert tree_digest(made) == backup_before
    assert not list(destination.parent.glob(".*partial"))


def test_restored_instance_opened_by_flipper_stays_equivalent(made, tmp_path):
    destination = tmp_path / "restored"
    restore_backup(made, destination)

    store = InventoryStore(destination / "flipper_inventory.db")
    assert [record.inventory_id for record in store.list()] == ["Q0001", "Q0002", "Q0005"]
    assert store.get("Q0005").acquisition_cost is None
    attachments = AttachmentService(store).list("Q0002")
    assert AttachmentService(store).path_for(attachments[0]).read_bytes() == JPEG

    assert verify_restored(made, destination).ok


@pytest.mark.parametrize("existing", ["empty-dir", "file", "populated"])
def test_existing_destination_is_refused(made, tmp_path, existing):
    destination = tmp_path / "dest"
    if existing == "file":
        destination.write_text("keep")
    else:
        destination.mkdir()
        if existing == "populated":
            (destination / "flipper_inventory.db").write_bytes(b"keep")
    before = tree_digest(tmp_path / "dest") if destination.is_dir() else destination.read_bytes()

    with pytest.raises(RestoreError, match="already exists"):
        restore_backup(made, destination)

    after = tree_digest(tmp_path / "dest") if destination.is_dir() else destination.read_bytes()
    assert after == before


def test_corrupt_backup_cannot_restore(made, tmp_path):
    victim = sorted((made / "attachments").iterdir())[0]
    victim.write_bytes(b"\xff\xd8\xfftampered")

    with pytest.raises(RestoreError, match="backup is invalid"):
        restore_backup(made, tmp_path / "dest")

    assert not (tmp_path / "dest").exists()
    assert [path.name for path in tmp_path.iterdir() if "partial" in path.name] == []


def test_newer_schema_backup_cannot_restore(made, tmp_path):
    with sqlite3.connect(made / "flipper_inventory.db") as connection:
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (99,))
    reseal(made)

    with pytest.raises(RestoreError, match="newer_schema"):
        restore_backup(made, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_partial_restore_cannot_masquerade_as_success(made, tmp_path, monkeypatch):
    original = service._copy_file
    calls = []

    def failing_copy(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise OSError("interrupted")
        return original(src, dst)

    monkeypatch.setattr(service, "_copy_file", failing_copy)
    with pytest.raises(OSError, match="interrupted"):
        restore_backup(made, tmp_path / "parent" / "dest")

    assert list((tmp_path / "parent").iterdir()) == []


def test_restore_that_fails_equivalence_is_discarded(made, tmp_path, monkeypatch):
    monkeypatch.setattr(
        service,
        "verify_restored",
        lambda backup, restored: service.VerificationResult(BackupStatus.INVALID, ("differs",)),
    )
    with pytest.raises(RestoreError, match="not equivalent"):
        restore_backup(made, tmp_path / "parent" / "dest")
    assert list((tmp_path / "parent").iterdir()) == []


def test_restore_refuses_active_and_ambiguous_locations(made, source, tmp_path):
    with pytest.raises(RestoreError, match="active database"):
        restore_backup(
            made,
            tmp_path / "new-data",
            active_database=tmp_path / "new-data" / "flipper_inventory.db",
        )
    with pytest.raises(RestoreError, match="active attachment"):
        restore_backup(made, source.attachments / "nested", active_database=source.database)
    with pytest.raises(RestoreError, match="inside the backup"):
        restore_backup(made, made / "attachments" / "restored")
    before = tree_digest(source.data_dir)
    with pytest.raises(RestoreError, match="already exists"):
        restore_backup(made, source.data_dir, active_database=source.database)
    assert tree_digest(source.data_dir) == before


def test_restore_does_not_change_active_configuration(made, source, tmp_path, monkeypatch):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(source.database))
    environment = dict(os.environ)
    before = tree_digest(source.data_dir)

    assert cli.main(["backup", "restore", str(made), "--destination", str(tmp_path / "r")]) == 0

    assert dict(os.environ) == environment
    assert tree_digest(source.data_dir) == before


def test_verify_restored_detects_divergence(made, tmp_path):
    destination = tmp_path / "restored"
    restore_backup(made, destination)
    (destination / "attachments" / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert (
        "unexpected file in attachments: extra.png" in verify_restored(made, destination).problems
    )
    (destination / "attachments" / "extra.png").unlink()

    InventoryStore(destination / "flipper_inventory.db").add(
        title="Added after restore", source="s", acquired_at="2026-09-21", acquisition_cost="1.00"
    )
    problems = verify_restored(made, destination).problems
    assert "logical fingerprint does not match the manifest" in problems
    assert "table inventory_items has 4 rows; manifest records 3" in problems


def test_verify_restored_reports_missing_database(made, tmp_path):
    result = verify_restored(made, tmp_path / "nothing")
    assert result.problems == ("database is missing or cannot be opened",)


# CLI


def test_cli_backup_verify_restore_and_fingerprint_round_trip(source, tmp_path, capsys):
    database = str(source.database)
    out = tmp_path / "out"

    assert cli.main(["backup", "--database", database, "create", "--output-dir", str(out)]) == 0
    created = capsys.readouterr().out
    assert "Created verified backup" in created
    assert "Credentials and secrets: excluded" in created
    backup = next(out.iterdir())

    assert cli.main(["backup", "verify", str(backup)]) == 0
    assert "Backup: valid" in capsys.readouterr().out

    restored = tmp_path / "restored"
    assert (
        cli.main(
            [
                "backup",
                "--database",
                database,
                "restore",
                str(backup),
                "--destination",
                str(restored),
            ]
        )
        == 0
    )
    assert "Flipper configuration was not changed" in capsys.readouterr().out

    assert cli.main(["backup", "verify-restore", str(backup), str(restored)]) == 0
    assert "logically equivalent" in capsys.readouterr().out

    assert cli.main(["backup", "fingerprint", database]) == 0
    source_output = capsys.readouterr().out
    assert cli.main(["backup", "fingerprint", str(restored / "flipper_inventory.db")]) == 0
    assert capsys.readouterr().out == source_output
    assert "Counters: Q 5 | S 1 | C 2" in source_output


def test_cli_verify_exit_codes_distinguish_failures(made, capsys):
    manifest = json.loads((made / "manifest.json").read_text())
    manifest["format_version"] = 7
    write_manifest(made, manifest, seal=True)
    assert cli.main(["backup", "verify", str(made)]) == 3
    assert "unsupported_format" in capsys.readouterr().out

    manifest["format_version"] = 1
    manifest["flipper_schema_version"] = 99
    write_manifest(made, manifest, seal=True)
    assert cli.main(["backup", "verify", str(made)]) == 4

    manifest["flipper_schema_version"] = CURRENT_SCHEMA_VERSION
    manifest["created_at"] = "tampered"
    write_manifest(made, manifest, seal=False)
    assert cli.main(["backup", "verify", str(made)]) == 1


def test_cli_errors_are_clean(source, tmp_path, capsys):
    assert (
        cli.main(
            [
                "backup",
                "--database",
                str(source.database),
                "create",
                "--output-dir",
                str(source.data_dir / "backups"),
            ]
        )
        == 1
    )
    assert "must be outside" in capsys.readouterr().err
    assert cli.main(["backup", "fingerprint", str(tmp_path / "absent.db")]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "absent.db").exists()


def test_cli_backup_uses_configured_storage_without_migrating(tmp_path, monkeypatch):
    database = tmp_path / "data" / "flipper_inventory.db"
    InventoryStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (14,))
    before = database.read_bytes()
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))

    assert cli.main(["backup", "create", "--output-dir", str(tmp_path / "out"), "--name", "b"]) == 0

    # An older-schema source is captured as-is; the backup never migrates it.
    assert database.read_bytes() == before
    manifest = json.loads((tmp_path / "out" / "b" / "manifest.json").read_text())
    assert manifest["flipper_schema_version"] == 13


def test_web_security_settings_never_enter_a_backup(tmp_path, monkeypatch):
    from web.passwords import hash_password

    password_hash = hash_password("correct horse battery staple", n=2**14)
    session_secret = "Zt4vQ9pLr2Xw8Kd3Nf6Hj1Ms5Bc7Ga0Ey-Ui_Oo4Pp2Rr6Tt8"
    public_origin = "https://flipper-private-origin.example.test"
    data_dir = tmp_path / "srv" / "data"
    populate(data_dir)
    settings = {
        "FLIPPER_WEB_SECURITY_MODE": "hosted",
        "FLIPPER_PASSWORD_HASH": password_hash,
        "FLIPPER_SESSION_SECRET": session_secret,
        "FLIPPER_PUBLIC_ORIGIN": public_origin,
    }
    (data_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FLIPPER_DATA_DIR", str(data_dir))

    assert cli.main(["backup", "create", "--output-dir", str(tmp_path / "out"), "--name", "b"]) == 0

    backup = tmp_path / "out" / "b"
    for path in backup.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for value in (password_hash, session_secret, public_origin, *settings):
                assert value.encode() not in content, (path.name, value[:12])
    assert verify_backup(backup).ok
