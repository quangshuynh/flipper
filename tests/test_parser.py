from parser.ai_enricher import enrich_specs_with_ai
from parser.extractor import extract_specs


def test_extracts_messy_hardware_and_extras():
    """
    extract messy hardware fields and included extras
    :returns: None
    """
    specs = extract_specs(
        "Gaming rig RTX 3060",
        "Ryzen 5 5600X, 16gb ddr4, 1TB NVMe SSD, 650W 80+ Gold PSU, "
        "B550 board, mid tower, AIO, Windows 11 Pro, monitor and keyboard included.",
    )

    assert specs.gpu == "RTX 3060"
    assert specs.cpu == "Ryzen 5 5600X"
    assert specs.ram == "16 GB DDR4"
    assert specs.storage == "1 TB NVME"
    assert specs.psu == "650W 80+ Gold"
    assert specs.motherboard == "B550"
    assert specs.os == "Windows 11 Pro"
    assert {"monitor", "keyboard"}.issubset(specs.extras)


def test_missing_specs_and_exclusions_are_explicit():
    """
    preserve missing fields and exclude unavailable extras
    :returns: None
    """
    specs = extract_specs("Desktop", "Works well. No monitor or keyboard included. Firm price.")

    assert specs.gpu == "not listed"
    assert specs.storage == "not listed"
    assert "monitor" not in specs.extras
    assert "keyboard" not in specs.extras
    assert "firm_price" in specs.flags
    assert "low_knowledge_seller" not in specs.flags


def test_ai_failure_returns_deterministic_specs(monkeypatch):
    """
    return deterministic specs when AI enrichment fails
    :param monkeypatch: pytest monkeypatch fixture
    :returns: None
    """
    base = extract_specs("RTX 3070 PC", "16GB RAM")
    monkeypatch.setattr(
        "parser.ai_enricher._call_groq_responses_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    enriched, summary = enrich_specs_with_ai("RTX 3070 PC", "16GB RAM", base)

    assert enriched == base
    assert "regex" in summary


def test_ai_fills_missing_values_without_overwriting_regex(monkeypatch):
    """
    fill missing values without overwriting deterministic extraction
    :param monkeypatch: pytest monkeypatch fixture
    :returns: None
    """
    base = extract_specs("RTX 3070 PC", "16GB RAM")
    monkeypatch.setattr(
        "parser.ai_enricher._call_groq_responses_api",
        lambda *args, **kwargs: {
            "gpu": "RTX 4090",
            "cpu": "Ryzen 5 5600X",
            "extras": "keyboard",
            "ai_summary": "partial",
        },
    )

    enriched, _ = enrich_specs_with_ai("RTX 3070 PC", "16GB RAM", base)

    assert enriched.gpu == "RTX 3070"
    assert enriched.cpu == "Ryzen 5 5600X"
    assert enriched.extras == ["k", "e", "y", "b", "o", "a", "r", "d"]
