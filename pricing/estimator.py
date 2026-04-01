"""
Estimate market value, buy price, sell price, profit, and score.
Expanded for 2026 relevance:
- RTX 50 / 40 / 30 / 20 series
- GTX 10 series including 1050 Ti
- More Intel and Ryzen CPUs
- DDR generation-aware RAM valuation
"""

import re

from models import ParsedSpecs


GPU_VALUES = {
    # RTX 50 series
    "RTX 5090": 2000,
    "RTX 5080": 1200,
    "RTX 5070 Ti": 800,
    "RTX 5070": 650,
    "RTX 5060 Ti": 430,
    "RTX 5060": 330,
    "RTX 5050": 220,

    # RTX 40 series
    "RTX 4090": 1400,
    "RTX 4080 Super": 950,
    "RTX 4080": 900,
    "RTX 4070 Ti Super": 700,
    "RTX 4070 Ti": 620,
    "RTX 4070 Super": 560,
    "RTX 4070": 500,
    "RTX 4060 Ti": 320,
    "RTX 4060": 260,
    "RTX 4050": 200,

    # RTX 30 series
    "RTX 3090 Ti": 700,
    "RTX 3090": 620,
    "RTX 3080 Ti": 500,
    "RTX 3080": 420,
    "RTX 3070 Ti": 340,
    "RTX 3070": 300,
    "RTX 3060 Ti": 260,
    "RTX 3060": 220,
    "RTX 3050": 150,

    # RTX 20 series
    "RTX 2080 Ti": 260,
    "RTX 2080 Super": 220,
    "RTX 2080": 200,
    "RTX 2070 Super": 190,
    "RTX 2070": 175,
    "RTX 2060 Super": 160,
    "RTX 2060": 140,

    # GTX 10 series
    "GTX 1080 Ti": 180,
    "GTX 1080": 140,
    "GTX 1070 Ti": 120,
    "GTX 1070": 100,
    "GTX 1060": 80,
    "GTX 1050 Ti": 60,
    "GTX 1050": 50,
    "GTX 1030": 30,

    # GTX 16 series
    "GTX 1660 Ti": 125,
    "GTX 1660 Super": 120,
    "GTX 1660": 110,
    "GTX 1650 Super": 90,
    "GTX 1650": 80,

    # AMD Radeon RX 7000
    "RX 7900 XTX": 760,
    "RX 7900 XT": 620,
    "RX 7800 XT": 420,
    "RX 7700 XT": 330,
    "RX 7600 XT": 240,
    "RX 7600": 200,

    # AMD Radeon RX 6000
    "RX 6950 XT": 500,
    "RX 6900 XT": 430,
    "RX 6800 XT": 360,
    "RX 6800": 300,
    "RX 6750 XT": 250,
    "RX 6700 XT": 230,
    "RX 6650 XT": 185,
    "RX 6600 XT": 170,
    "RX 6600": 150,

    # AMD Radeon RX 5000 / older flip staples
    "RX 5700 XT": 130,
    "RX 5700": 115,
    "RX 5600 XT": 100,
    "RX 590": 80,
    "RX 580": 70,
    "RX 570": 60,

    # Intel Arc
    "Arc A770": 220,
    "Arc A750": 180,
    "Arc A580": 140,
    "Arc A380": 80,
}


CPU_VALUES = {
    # AMD Ryzen 9000 / X3D
    "Ryzen 9 9950X3D": 650,
    "Ryzen 9 9950X": 500,
    "Ryzen 9 9900X3D": 560,
    "Ryzen 9 9900X": 360,
    "Ryzen 7 9800X3D": 400,
    "Ryzen 7 9700X": 240,
    "Ryzen 5 9600X": 180,

    # AMD Ryzen 7000 / X3D
    "Ryzen 9 7950X3D": 430,
    "Ryzen 9 7950X": 320,
    "Ryzen 9 7900X3D": 360,
    "Ryzen 9 7900X": 260,
    "Ryzen 7 7800X3D": 300,
    "Ryzen 7 7700X": 180,
    "Ryzen 7 7700": 165,
    "Ryzen 5 7600X": 145,
    "Ryzen 5 7600": 130,

    # AMD Ryzen 5000
    "Ryzen 9 5950X": 220,
    "Ryzen 9 5900X": 170,
    "Ryzen 7 5800X3D": 200,
    "Ryzen 7 5800X": 130,
    "Ryzen 7 5700X": 120,
    "Ryzen 5 5600X": 90,
    "Ryzen 5 5600": 85,

    # AMD Ryzen 3000
    "Ryzen 9 3900X": 110,
    "Ryzen 7 3800X": 80,
    "Ryzen 7 3700X": 75,
    "Ryzen 5 3600X": 60,
    "Ryzen 5 3600": 55,

    # Intel Core Ultra / recent Intel
    "Ultra 9 285K": 420,
    "Ultra 7 265K": 260,
    "Ultra 7 265KF": 245,
    "Ultra 5 245K": 190,
    "Ultra 5 245KF": 175,

    # Intel 14th / 13th / 12th gen
    "i9-14900K": 380,
    "i7-14700K": 280,
    "i5-14600K": 200,
    "i9-13900K": 320,
    "i7-13700K": 240,
    "i5-13600K": 170,
    "i7-12700K": 190,
    "i7-12700": 180,
    "i5-12600K": 140,
    "i5-12400": 100,

    # Intel 11th / 10th gen
    "i9-11900K": 150,
    "i7-11700K": 130,
    "i5-11600K": 90,
    "i9-10900K": 170,
    "i7-10700K": 120,
    "i7-10700": 110,
    "i5-10600K": 85,
    "i5-10400": 60,

    # Older but common
    "i7-9700K": 100,
    "i7-8700K": 90,
    "i7-7700K": 65,
    "i5-9600K": 70,
    "i5-8400": 45,
    "i5-7500": 30,

    # Xeon / broad fallbacks
    "Xeon E5": 25,
    "Xeon E3": 20,
    "Xeon": 40,

    # Generic fallbacks
    "Ryzen 9": 180,
    "Ryzen 7": 100,
    "Ryzen 5": 60,
    "Ryzen 3": 35,
    "i9": 160,
    "i7": 85,
    "i5": 50,
    "i3": 25,
}


RAM_VALUES = {
    # Explicit DDR generation anchors
    "128 GB DDR5": 700,
    "96 GB DDR5": 500,
    "64 GB DDR5": 420,
    "48 GB DDR5": 320,
    "32 GB DDR5": 270,   # user reference
    "24 GB DDR5": 180,
    "16 GB DDR5": 100,
    "8 GB DDR5": 45,

    "128 GB DDR4": 180,
    "64 GB DDR4": 95,
    "48 GB DDR4": 75,
    "32 GB DDR4": 55,
    "24 GB DDR4": 40,
    "16 GB DDR4": 28,
    "8 GB DDR4": 10,
    "4 GB DDR4": 5,

    "64 GB DDR3": 35,
    "32 GB DDR3": 22,
    "16 GB DDR3": 12,
    "8 GB DDR3": 6,
    "4 GB DDR3": 3,

    "16 GB DDR2": 8,
    "8 GB DDR2": 5,
    "4 GB DDR2": 3,

    "4 GB DDR1": 3,
    "2 GB DDR1": 2,
    "DDR5": 60,
    "DDR4": 18,
    "DDR3": 10,
    "DDR2": 5,
    "DDR1": 2,

    # Generic non-generation capacities as fallback
    "128 GB RAM": 180,
    "96 GB RAM": 130,
    "64 GB RAM": 95,
    "48 GB RAM": 70,
    "32 GB RAM": 55,
    "24 GB RAM": 40,
    "16 GB RAM": 28,
    "12 GB RAM": 20,
    "8 GB RAM": 10,
    "4 GB RAM": 5,

    "128GB RAM": 180,
    "96GB RAM": 130,
    "64GB RAM": 95,
    "48GB RAM": 70,
    "32GB RAM": 55,
    "24GB RAM": 40,
    "16GB RAM": 28,
    "12GB RAM": 20,
    "8GB RAM": 10,
    "4GB RAM": 5,

    "128 GB": 180,
    "96 GB": 130,
    "64 GB": 95,
    "48 GB": 70,
    "32 GB": 55,
    "24 GB": 40,
    "16 GB": 28,
    "12 GB": 20,
    "8 GB": 10,
    "4 GB": 5,
}


EXTRA_VALUES = {
    "monitor": 50,
    "keyboard": 10,
    "mouse": 10,
    "headset": 15,
    "speakers": 15,
    "webcam": 15,
    "mic": 20,
}


GPU_PREFIX_BY_MODEL = {
    "5090": "RTX",
    "5080": "RTX",
    "5070": "RTX",
    "5060": "RTX",
    "5050": "RTX",
    "4090": "RTX",
    "4080": "RTX",
    "4070": "RTX",
    "4060": "RTX",
    "4050": "RTX",
    "3090": "RTX",
    "3080": "RTX",
    "3070": "RTX",
    "3060": "RTX",
    "3050": "RTX",
    "2080": "RTX",
    "2070": "RTX",
    "2060": "RTX",
    "1660": "GTX",
    "1650": "GTX",
    "1080": "GTX",
    "1070": "GTX",
    "1060": "GTX",
    "1050": "GTX",
    "1030": "GTX",
}


def _normalize_spaces(value: str) -> str:
    """
    Normalize spacing for dictionary matching.

    :param value: Input text.
    :returns: Normalized text.
    """
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_gpu_value(value: str) -> str:
    """
    Canonicalize messy GPU strings for pricing lookup.

    :param value: Value to search for.
    :returns: Canonical GPU value.
    """
    text = _normalize_spaces(value)
    if text == "not listed":
        return text

    cleaned = re.sub(
        r"\b(NVIDIA|GEFORCE|AMD|RADEON|EVGA|ASUS|MSI|GIGABYTE|ZOTAC|PNY|SAPPHIRE|XFX|POWERCOLOR|ASROCK|FOUNDERS\s+EDITION|FE)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = _normalize_spaces(cleaned)

    arc_match = re.search(r"\bArc\s*(A(?:770|750|580|380))\b", cleaned, flags=re.IGNORECASE)
    if arc_match:
        return f"Arc {arc_match.group(1).upper()}"

    amd_match = re.search(
        r"\bRX\s*(?P<model>7900|7800|7700|7600|6950|6900|6800|6750|6700|6650|6600|5700|5600|590|580|570)"
        r"(?P<suffix>(?:\s*(?:XTX|XT))?)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if amd_match:
        suffix_tokens = re.findall(r"xtx|xt", amd_match.group("suffix") or "", flags=re.IGNORECASE)
        suffix = f" {suffix_tokens[0].upper()}" if suffix_tokens else ""
        return f"RX {amd_match.group('model')}{suffix}"

    nvidia_match = re.search(
        r"\b(?:(?P<prefix>RTX|GTX)\s*)?"
        r"(?P<model>5090|5080|5070|5060|5050|4090|4080|4070|4060|4050|3090|3080|3070|3060|3050|2080|2070|2060|1660|1650|1080|1070|1060|1050|1030)"
        r"(?P<suffix>(?:\s*(?:Ti|Super)){0,2})"
        r"(?:\s*\d{1,2}\s*GB)?\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if nvidia_match:
        prefix = (nvidia_match.group("prefix") or GPU_PREFIX_BY_MODEL[nvidia_match.group("model")]).upper()
        suffix_tokens = {token.lower() for token in re.findall(r"ti|super", nvidia_match.group("suffix") or "", flags=re.IGNORECASE)}
        suffix = ""
        if "ti" in suffix_tokens and "super" in suffix_tokens:
            suffix = " Ti Super"
        elif "ti" in suffix_tokens:
            suffix = " Ti"
        elif "super" in suffix_tokens:
            suffix = " Super"
        return f"{prefix} {nvidia_match.group('model')}{suffix}"

    return cleaned


def _normalize_cpu_value(value: str) -> str:
    """
    Canonicalize messy CPU strings for pricing lookup.

    :param value: Value to search for.
    :returns: Canonical CPU value.
    """
    text = _normalize_spaces(value)
    if text == "not listed":
        return text

    ultra_match = re.search(
        r"\b(?:Core\s+)?Ultra\s*(?P<tier>[579])\s*(?P<model>\d{3}[A-Za-z]{0,3})\b",
        text,
        flags=re.IGNORECASE,
    )
    if ultra_match:
        return f"Ultra {ultra_match.group('tier')} {ultra_match.group('model').upper()}"

    intel_match = re.search(
        r"\b(?P<tier>i[3579])\s*[- ]?\s*(?P<model>\d{4,5}[A-Za-z]{0,3})\b",
        text,
        flags=re.IGNORECASE,
    )
    if intel_match:
        return f"{intel_match.group('tier').lower()}-{intel_match.group('model').upper()}"

    ryzen_match = re.search(
        r"\bRyzen\s*(?P<tier>[3579])\s*(?P<model>\d{4,5}[A-Za-z0-9]{0,4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if ryzen_match:
        return f"Ryzen {ryzen_match.group('tier')} {ryzen_match.group('model').upper()}"

    xeon_match = re.search(r"\bXeon(?:\s+(?P<model>[A-Za-z0-9-]+))?\b", text, flags=re.IGNORECASE)
    if xeon_match:
        model = xeon_match.group("model")
        return f"Xeon {model.upper()}".strip() if model else "Xeon"

    loose_match = re.search(r"\b(Ryzen\s?[3579]|i[3579])\b", text, flags=re.IGNORECASE)
    if loose_match:
        return loose_match.group(1).replace("  ", " ")

    return text


def _normalize_ram_value(value: str) -> str:
    """
    Canonicalize RAM strings for pricing lookup.

    :param value: Value to search for.
    :returns: Canonical RAM value.
    """
    text = _normalize_spaces(value)
    if text == "not listed":
        return text

    kit_match = re.search(r"\b(?P<count>\d+)\s*[xX]\s*(?P<size>\d+)\s*GB\b", text, flags=re.IGNORECASE)
    ddr_match = re.search(r"\b(?P<ddr>DDR[1-5])\b", text, flags=re.IGNORECASE)
    if kit_match:
        total_gb = int(kit_match.group("count")) * int(kit_match.group("size"))
        if ddr_match:
            return f"{total_gb} GB {ddr_match.group('ddr').upper()}"
        return f"{total_gb} GB RAM"

    capacity_then_ddr = re.search(
        r"\b(?P<size>\d+)\s*GB\b(?:\s*(?:RAM|memory))?(?:\s*[-@+/.a-z0-9]{0,20})?\b(?P<ddr>DDR[1-5])\b",
        text,
        flags=re.IGNORECASE,
    )
    if capacity_then_ddr:
        return f"{capacity_then_ddr.group('size')} GB {capacity_then_ddr.group('ddr').upper()}"

    ddr_then_capacity = re.search(
        r"\b(?P<ddr>DDR[1-5])\b(?:\s*[-@+/.a-z0-9]{0,20})?\b(?P<size>\d+)\s*GB\b",
        text,
        flags=re.IGNORECASE,
    )
    if ddr_then_capacity:
        return f"{ddr_then_capacity.group('size')} GB {ddr_then_capacity.group('ddr').upper()}"

    capacity_only = re.search(r"\b(?P<size>\d+)\s*GB\b(?:\s*(?:RAM|memory))?\b", text, flags=re.IGNORECASE)
    if capacity_only:
        return f"{capacity_only.group('size')} GB RAM"

    if ddr_match:
        return ddr_match.group("ddr").upper()

    return text


def _lookup_partial(
    value: str,
    value_map: dict[str, int],
    default: int = 0,
    normalizer=None,
) -> int:
    """
    Match exact first, then partial containment after normalization.

    :param value: Value to search for.
    :param value_map: Map of known values to prices.
    :param default: Default if not found.
    :param normalizer: Optional component-specific normalizer.
    :returns: Numeric estimated value.
    """
    normalize = normalizer or _normalize_spaces
    normalized = normalize(value)
    normalized_map: dict[str, int] = {}

    for key, amount in value_map.items():
        normalized_key = normalize(key)
        normalized_map.setdefault(normalized_key, amount)

    if normalized in normalized_map:
        return normalized_map[normalized]

    normalized_lower = normalized.lower()
    for key, amount in normalized_map.items():
        key_lower = key.lower()
        if key_lower in normalized_lower or normalized_lower in key_lower:
            return amount

    return default


def _estimate_storage_value(storage: str) -> int:
    """
    Estimate storage value with type + size awareness.

    :param storage: Parsed storage text.
    :returns: Numeric estimated value.
    """
    if storage == "not listed":
        return 0

    text = storage.upper()
    value = 0

    if "NVME" in text:
        value += 50
    elif "SSD" in text:
        value += 35
    elif "HDD" in text:
        value += 15
    else:
        value += 20

    if "4 TB" in text or "4TB" in text:
        value += 45
    elif "2 TB" in text or "2TB" in text:
        value += 30
    elif "1 TB" in text or "1TB" in text:
        value += 20
    elif "500 GB" in text or "512" in text:
        value += 10
    elif "250 GB" in text or "256" in text:
        value += 5

    return value


def _estimate_psu_value(psu: str) -> int:
    """
    Estimate PSU value.

    :param psu: Parsed PSU text.
    :returns: Numeric estimated value.
    """
    if psu == "not listed":
        return 0

    text = psu.upper()
    value = 15

    if "GOLD" in text:
        value += 20
    elif "PLATINUM" in text:
        value += 28
    elif "TITANIUM" in text:
        value += 35
    elif "SILVER" in text:
        value += 12
    elif "BRONZE" in text:
        value += 8

    return value


def estimate_market_value(specs: ParsedSpecs) -> float:
    """
    Estimate local resale/market value from extracted specs.

    :param specs: Parsed specs.
    :returns: Estimated market value.
    """
    value = 40

    gpu_value = _lookup_partial(specs.gpu, GPU_VALUES, 0, normalizer=_normalize_gpu_value)
    cpu_value = _lookup_partial(specs.cpu, CPU_VALUES, 0, normalizer=_normalize_cpu_value)
    ram_value = _lookup_partial(specs.ram, RAM_VALUES, 0, normalizer=_normalize_ram_value)

    value += gpu_value
    value += cpu_value
    value += ram_value

    value += _estimate_storage_value(specs.storage)
    value += _estimate_psu_value(specs.psu)

    if specs.motherboard != "not listed":
        value += 25
    if specs.case != "not listed":
        value += 25
    if specs.cpu_cooler != "not listed":
        value += 20
    if specs.os != "not listed":
        value += 15

    for extra in specs.extras:
        value += EXTRA_VALUES.get(extra, 0)

    # Fallback for vaguely-listed systems with a GPU but no direct map hit
    if specs.gpu != "not listed" and gpu_value == 0:
        value += 50

    # Fallback for vaguely-listed systems with a CPU but no direct map hit
    if specs.cpu != "not listed" and cpu_value == 0:
        value += 25

    # Fallback for vaguely-listed RAM but no direct map hit
    if specs.ram != "not listed" and ram_value == 0:
        value += 10

    return round(value, 2)


def calculate_pricing(estimated_value: float) -> tuple[float, float]:
    """
    Compute ideal buy and ideal sell prices.

    :param estimated_value: Estimated market value.
    :returns: Tuple of ideal buy, ideal sell.
    """
    ideal_buy = estimated_value * 0.68
    ideal_sell = estimated_value * 0.95
    return round(ideal_buy, 2), round(ideal_sell, 2)


def score_deal(
    asking_price: float,
    estimated_value: float,
    specs: ParsedSpecs,
    distance_miles: float | None
) -> tuple[int, float]:
    """
    Score the deal and estimate profit.

    :param asking_price: Seller asking price.
    :param estimated_value: Estimated market value.
    :param specs: Parsed specs.
    :param distance_miles: Distance from target location.
    :returns: Tuple of score and estimated profit.
    """
    ideal_buy, ideal_sell = calculate_pricing(estimated_value)
    profit = round(ideal_sell - asking_price, 2)
    score = 0

    if asking_price <= ideal_buy:
        score += 30
    elif asking_price <= estimated_value * 0.8:
        score += 18

    if profit >= 250:
        score += 30
    elif profit >= 150:
        score += 22
    elif profit >= 100:
        score += 15
    elif profit >= 50:
        score += 8

    if "low_knowledge_seller" in specs.flags:
        score += 15

    if "negotiable" in specs.flags:
        score += 8

    if specs.gpu != "not listed":
        score += 8
    if specs.cpu != "not listed":
        score += 7
    if specs.ram != "not listed":
        score += 5
    if specs.storage != "not listed":
        score += 4

    if specs.extras:
        score += min(len(specs.extras) * 2, 8)

    if distance_miles is not None:
        if distance_miles <= 20:
            score += 12
        elif distance_miles <= 50:
            score += 8
        elif distance_miles <= 100:
            score += 4
        else:
            score -= 25

    return score, profit
