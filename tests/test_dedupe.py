def test_dedupe_initialization_creates_parent_directory(tmp_path, monkeypatch):
    """
    create the dedupe database parent directory during initialization
    :param tmp_path: pytest temporary directory fixture
    :param monkeypatch: pytest monkeypatch fixture
    :returns: None
    """
    from utils import dedupe

    monkeypatch.setattr(dedupe, "DB_PATH", tmp_path / "nested" / "seen.db")
    dedupe.init_db()
    assert dedupe.has_seen("missing") is False
    dedupe.mark_seen("listing")
    assert dedupe.has_seen("listing") is True
