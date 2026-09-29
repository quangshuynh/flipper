import sqlite3
from dataclasses import replace

import pytest

from inventory.attachments import MAX_ATTACHMENT_BYTES, AttachmentService
from inventory.store import (
    AttachmentNotFoundError,
    AttachmentValidationError,
    InventoryNotFoundError,
    InventoryStore,
)


JPEG = b"\xff\xd8\xffsynthetic-jpeg-bytes"
PDF = b"%PDF-1.7\nsynthetic pdf\n%%EOF"


def item(store, title="Item"):
    return store.add(
        title=title,
        source="test sale",
        acquired_at="2026-09-01",
        acquisition_cost="10.00",
    )


def source(tmp_path, name="photo.jpg", contents=JPEG):
    path = tmp_path / name
    path.write_bytes(contents)
    return path


def test_add_preserves_bytes_and_uses_durable_controlled_identity(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    record = item(store)
    original = source(tmp_path, "estate photo.jpg")
    service = AttachmentService(store)

    attachment = service.add(record.inventory_id, original, category="product_photo")

    assert attachment.original_filename == "estate photo.jpg"
    assert attachment.stored_filename != attachment.original_filename
    assert attachment.attachment_id in attachment.stored_filename
    assert "/" not in attachment.stored_filename and "\\" not in attachment.stored_filename
    assert service.path_for(attachment).read_bytes() == JPEG
    assert str(tmp_path.resolve()) not in attachment.stored_filename


def test_same_original_filename_can_be_attached_more_than_once(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    record = item(store)
    original = source(tmp_path)
    service = AttachmentService(store)

    first = service.add(record.inventory_id, original)
    second = service.add(record.inventory_id, original)

    assert first.attachment_id != second.attachment_id
    assert first.stored_filename != second.stored_filename
    assert len(service.list(record.inventory_id)) == 2


def test_add_rejects_unknown_item_missing_unsupported_and_oversized_files(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item(store)
    service = AttachmentService(store)
    valid = source(tmp_path)
    with pytest.raises(InventoryNotFoundError):
        service.add("Q9999", valid)
    with pytest.raises(AttachmentValidationError, match="does not exist"):
        service.add("Q0001", tmp_path / "missing.jpg")
    with pytest.raises(AttachmentValidationError, match="unsupported"):
        service.add("Q0001", source(tmp_path, "script.exe", b"MZ"))
    oversized = source(tmp_path, "large.pdf", PDF)
    with oversized.open("r+b") as handle:
        handle.truncate(MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(AttachmentValidationError, match="25 MiB"):
        service.add("Q0001", oversized)


@pytest.mark.parametrize(
    ("name", "contents"),
    [("fake.jpg", PDF), ("fake.pdf", JPEG), ("fake.png", b"not a png")],
)
def test_extension_must_match_file_signature(tmp_path, name, contents):
    store = InventoryStore(tmp_path / "inventory.db")
    item(store)
    with pytest.raises(AttachmentValidationError, match="contents"):
        AttachmentService(store).add("Q0001", source(tmp_path, name, contents))


def test_list_is_scoped_and_removal_verifies_ownership(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first_item = item(store, "First")
    second_item = item(store, "Second")
    service = AttachmentService(store)
    first = service.add(first_item.inventory_id, source(tmp_path, "one.jpg"))
    second = service.add(second_item.inventory_id, source(tmp_path, "two.pdf", PDF))

    assert service.list(first_item.inventory_id) == [first]
    with pytest.raises(AttachmentNotFoundError):
        service.remove(second_item.inventory_id, first.attachment_id)
    assert service.path_for(first).exists()
    removed = service.remove(first_item.inventory_id, first.attachment_id)
    assert removed == first
    assert not (service.root / first.stored_filename).exists()
    assert service.list(first_item.inventory_id) == []
    assert service.list(second_item.inventory_id) == [second]


def test_traversal_storage_identity_is_rejected(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    record = item(store)
    service = AttachmentService(store)
    attachment = service.add(record.inventory_id, source(tmp_path, "..photo.jpg"))
    assert attachment.original_filename == "..photo.jpg"
    with pytest.raises(AttachmentValidationError, match="unsafe"):
        service.path_for(replace(attachment, stored_filename="../escape.jpg"))


def test_database_insert_failure_cleans_final_and_staging_files(tmp_path, monkeypatch):
    store = InventoryStore(tmp_path / "inventory.db")
    item(store)
    service = AttachmentService(store)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic insert failure")

    monkeypatch.setattr(store, "add_attachment_metadata", fail)
    with pytest.raises(sqlite3.OperationalError):
        service.add("Q0001", source(tmp_path))
    assert list(service.root.iterdir()) == []
    assert store.list_attachments("Q0001") == []


def test_file_copy_failure_leaves_no_metadata_or_files(tmp_path, monkeypatch):
    store = InventoryStore(tmp_path / "inventory.db")
    item(store)
    service = AttachmentService(store)

    def fail(*args, **kwargs):
        raise OSError("synthetic copy failure")

    monkeypatch.setattr("inventory.attachments.shutil.copyfileobj", fail)
    with pytest.raises(OSError):
        service.add("Q0001", source(tmp_path))
    assert list(service.root.iterdir()) == []
    assert store.list_attachments("Q0001") == []


def test_metadata_delete_failure_restores_file_and_row(tmp_path, monkeypatch):
    store = InventoryStore(tmp_path / "inventory.db")
    record = item(store)
    service = AttachmentService(store)
    attachment = service.add(record.inventory_id, source(tmp_path))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic delete failure")

    monkeypatch.setattr(store, "remove_attachment_metadata", fail)
    with pytest.raises(sqlite3.OperationalError):
        service.remove(record.inventory_id, attachment.attachment_id)
    assert service.path_for(attachment).read_bytes() == JPEG
    assert store.get_attachment(attachment.attachment_id) == attachment


def test_archive_and_sale_retain_attachments_and_database_delete_is_restricted(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    archived = item(store, "Archived")
    sold = item(store, "Sold")
    service = AttachmentService(store)
    archive_attachment = service.add(archived.inventory_id, source(tmp_path, "archive.jpg"))
    sold_attachment = service.add(sold.inventory_id, source(tmp_path, "sold.pdf", PDF))

    store.transition_status(archived.inventory_id, "archived")
    store.transition_status(sold.inventory_id, "listed")
    store.transition_status(sold.inventory_id, "sold")
    assert service.list(archived.inventory_id) == [archive_attachment]
    assert service.list(sold.inventory_id) == [sold_attachment]

    with store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM inventory_items WHERE inventory_id = ?", (archived.inventory_id,)
            )
    assert service.path_for(archive_attachment).exists()


def test_v8_database_migrates_to_attachment_schema_without_fake_rows(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    record = item(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE inventory_attachments")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")

    store.initialize()

    assert store.get(record.inventory_id) == record
    assert store.list_attachments(record.inventory_id) == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 9"
        ).fetchone() == (9,)
