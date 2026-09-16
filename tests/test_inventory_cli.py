import main


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
            "--status",
            "listed",
        )
        == 0
    )
    assert "Q0001" in capsys.readouterr().out
    assert command(database, "list") == 0
    assert "Q0001 | listed | qty 1 | $20.00 | Recorder" in capsys.readouterr().out
    assert command(database, "show", "Q0001") == 0
    output = capsys.readouterr().out
    assert "Acquisition" in output
    assert "Marketplace linkage" in output
    assert "Listing/item ID: 123" in output


def test_unknown_item_fails_cleanly(tmp_path, capsys):
    assert command(tmp_path / "inventory.db", "show", "Q9999") == 1
    assert "was not found" in capsys.readouterr().err
