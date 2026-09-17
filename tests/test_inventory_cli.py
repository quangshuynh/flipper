import main
import pytest


def command(database, *arguments):
    return main.main(["inventory", "--database", str(database), *arguments])


def test_add_list_and_show(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    assert (
        command(
            database,
            "add",
            "--title",
            "Recorder",
            "--source",
            "local sale",
            "--acquired-at",
            "2026-09-01",
            "--cost",
            "20.00",
            "--marketplace",
            "eBay",
            "--marketplace-item-id",
            "123",
            "--marketplace-sku",
            "Q0001",
        )
        == 0
    )
    assert "Q0001" in capsys.readouterr().out
    assert command(database, "list") == 0
    assert "Q0001 | acquired | qty 1 | $20.00 | Recorder" in capsys.readouterr().out
    assert command(database, "show", "Q0001") == 0
    output = capsys.readouterr().out
    assert "Acquisition" in output
    assert "Marketplace linkage" in output
    assert "Listing/item ID: 123" in output


def test_unknown_item_fails_cleanly(tmp_path, capsys):
    assert command(tmp_path / "inventory.db", "show", "Q9999") == 1
    assert "was not found" in capsys.readouterr().err


def test_partial_update_prints_result_and_preserves_other_fields(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    assert (
        command(
            database,
            "add",
            "--title",
            "Recorder",
            "--source",
            "local sale",
            "--acquired-at",
            "2026-09-01",
            "--cost",
            "20.00",
        )
        == 0
    )
    capsys.readouterr()
    assert command(database, "update", "Q0001", "--notes", "tested") == 0
    assert "Updated inventory item Q0001" in capsys.readouterr().out
    assert command(database, "show", "Q0001") == 0
    output = capsys.readouterr().out
    assert "Notes: tested" in output
    assert "Source: local sale" in output


def test_cli_can_clear_marketplace_field(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    assert (
        command(
            database,
            "add",
            "--title",
            "Recorder",
            "--source",
            "sale",
            "--acquired-at",
            "2026-09-01",
            "--cost",
            "20",
            "--marketplace-item-id",
            "123",
        )
        == 0
    )
    assert command(database, "update", "Q0001", "--marketplace-item-id", "") == 0
    assert command(database, "show", "Q0001") == 0
    assert "Listing/item ID: none" in capsys.readouterr().out


def test_cli_does_not_offer_inventory_id_mutation(tmp_path):
    with pytest.raises(SystemExit):
        command(tmp_path / "inventory.db", "update", "Q0001", "--inventory-id", "Q0002")


def test_status_command_applies_lifecycle_and_show_displays_timestamps(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    assert (
        command(
            database,
            "add",
            "--title",
            "Recorder",
            "--source",
            "sale",
            "--acquired-at",
            "2026-09-01",
            "--cost",
            "20",
        )
        == 0
    )
    assert command(database, "status", "Q0001", "listed") == 0
    assert "Inventory item Q0001: listed" in capsys.readouterr().out
    assert command(database, "show", "Q0001") == 0
    output = capsys.readouterr().out
    assert "Listed at (UTC):" in output
    assert "Listed at (UTC): none" not in output
    assert "Sold at (UTC): none" in output


def test_update_cli_does_not_offer_status_bypass(tmp_path):
    with pytest.raises(SystemExit):
        command(tmp_path / "inventory.db", "update", "Q0001", "--status", "listed")


def test_attachment_cli_add_list_and_remove(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xffsynthetic")
    assert (
        command(
            database,
            "add",
            "--title",
            "Recorder",
            "--source",
            "sale",
            "--acquired-at",
            "2026-09-01",
            "--cost",
            "20",
        )
        == 0
    )
    capsys.readouterr()

    assert command(database, "attach", "Q0001", str(photo), "--category", "product_photo") == 0
    output = capsys.readouterr().out
    attachment_id = output.splitlines()[0].split()[1]
    assert "product_photo | photo.jpg | image/jpeg" in output
    assert command(database, "attachments", "Q0001") == 0
    assert attachment_id in capsys.readouterr().out
    assert command(database, "remove-attachment", "Q0001", attachment_id) == 0
    assert "Removed attachment" in capsys.readouterr().out
    assert command(database, "attachments", "Q0001") == 0
    assert "No attachments found" in capsys.readouterr().out
