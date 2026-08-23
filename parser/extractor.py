"""
Extract structured specs and signals from listing text.
"""

import re

from models import ParsedSpecs


EXTRA_PATTERNS = {
    "monitor": r"\bmonitor\b",
    "keyboard": r"\bkeyboard\b",
    "mouse": r"\bmice\b|\bmouse\b|\bmouses\b",
    "headset": r"\bheadset\b",
    "speakers": r"\bspeakers?\b",
    "desk": r"\bdesk\b",
    "mic": r"\bmic\b|\bmicrophone\b",
    "webcam": r"\bwebcam\b",
    "chair": r"\bchair\b",
}

LOW_KNOWLEDGE_PATTERNS = [
    "don't know much about computers",
    "dont know much about computers",
    "don't know much",
    "dont know much",
    "not sure what it has",
    "not sure on specs",
    "friend built it",
    "just want gone",
    "moving sale",
    "need gone",
    "don't know specs",
    "dont know specs",
]

OS_PATTERNS = [
    r"\bWindows\s?11(?:\s?(?:Home|Pro))?\b",
    r"\bWindows\s?10(?:\s?(?:Home|Pro))?\b",
    r"\bLinux\b",
    r"\bUbuntu\b",
]

NVIDIA_GPU_PREFIX_BY_MODEL = {
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

AMD_RX_MODELS = {
    "7900",
    "7800",
    "7700",
    "7600",
    "6950",
    "6900",
    "6800",
    "6750",
    "6700",
    "6650",
    "6600",
    "5700",
    "5600",
    "590",
    "580",
    "570",
}


def _normalize_spaces(value: str) -> str:
    """
    Collapse repeated whitespace.

    :param value: Raw text.
    :returns: Normalized text.
    """
    return re.sub(r"\s+", " ", value).strip()


def _candidate_segments(text: str) -> list[str]:
    """
    Split listing text into smaller segments for localized matching.

    :param text: Raw listing text.
    :returns: Ordered candidate segments.
    """
    normalized = text.replace("\r", "\n")
    parts = re.split(r"[\n,;|]+", normalized)
    segments = [_normalize_spaces(part) for part in parts if _normalize_spaces(part)]
    combined = _normalize_spaces(normalized)

    if combined and combined not in segments:
        segments.append(combined)

    return segments


def _clean_component_segment(segment: str) -> str:
    """
    Remove list bullets and common field labels from a component segment.

    :param segment: Raw segment text.
    :returns: Cleaned segment text.
    """
    cleaned = re.sub(r"^[\-\*\u2022]\s*", "", segment)
    cleaned = re.sub(
        r"^(?:gpu|cpu|ram|storage|motherboard|mobo|cpu cooler|cooler|case|psu|"
        r"power supply|os)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _normalize_spaces(cleaned)


def _ordered_suffix(suffix_text: str) -> str:
    """
    Canonicalize GPU suffix ordering.

    :param suffix_text: Raw suffix text.
    :returns: Canonical suffix text.
    """
    suffixes = {
        token.lower() for token in re.findall(r"ti|super|xtx|xt", suffix_text, re.IGNORECASE)
    }

    if "xtx" in suffixes:
        return " XTX"
    if "xt" in suffixes:
        return " XT"
    if "ti" in suffixes and "super" in suffixes:
        return " Ti Super"
    if "ti" in suffixes:
        return " Ti"
    if "super" in suffixes:
        return " Super"

    return ""


def _extract_gpu(text: str) -> str:
    """
    Extract and normalize a GPU model.

    :param text: Listing text.
    :returns: Canonical GPU string or 'not listed'.
    """
    combined = _normalize_spaces(text)

    arc_match = re.search(r"\bArc\s*(A(?:770|750|580|380))\b", combined, flags=re.IGNORECASE)
    if arc_match:
        return f"Arc {arc_match.group(1).upper()}"

    amd_match = re.search(
        r"\b(?:AMD\s+)?(?:Radeon\s+)?RX\s*"
        r"(?P<model>7900|7800|7700|7600|6950|6900|6800|6750|6700|6650|6600|5700|5600|590|580|570)"
        r"(?P<suffix>(?:\s*(?:XTX|XT))?)\b",
        combined,
        flags=re.IGNORECASE,
    )
    if amd_match:
        model = amd_match.group("model")
        suffix = _ordered_suffix(amd_match.group("suffix") or "")
        return f"RX {model}{suffix}"

    nvidia_match = re.search(
        r"\b(?:NVIDIA\s+)?(?:GeForce\s+)?(?:(?P<prefix>RTX|GTX)\s*)?"
        r"(?P<model>5090|5080|5070|5060|5050|4090|4080|4070|4060|4050|3090|3080|3070|3060|3050|2080|2070|2060|1660|1650|1080|1070|1060|1050|1030)"
        r"(?P<suffix>(?:\s*(?:Ti|Super)){0,2})"
        r"(?:\s*\d{1,2}\s*GB)?\b",
        combined,
        flags=re.IGNORECASE,
    )
    if nvidia_match:
        model = nvidia_match.group("model")
        prefix = (nvidia_match.group("prefix") or NVIDIA_GPU_PREFIX_BY_MODEL[model]).upper()
        suffix = _ordered_suffix(nvidia_match.group("suffix") or "")
        return f"{prefix} {model}{suffix}"

    return "not listed"


def _extract_cpu(text: str) -> str:
    """
    Extract and normalize a CPU model.

    :param text: Listing text.
    :returns: Canonical CPU string or 'not listed'.
    """
    combined = _normalize_spaces(text)

    ultra_match = re.search(
        r"\b(?:Core\s+)?Ultra\s*(?P<tier>[579])\s*(?P<model>\d{3}[A-Za-z]{0,3})\b",
        combined,
        flags=re.IGNORECASE,
    )
    if ultra_match:
        tier = ultra_match.group("tier")
        model = ultra_match.group("model").upper()
        return f"Ultra {tier} {model}"

    intel_match = re.search(
        r"\b(?P<tier>i[3579])\s*[- ]?\s*(?P<model>\d{4,5}[A-Za-z]{0,3})\b",
        combined,
        flags=re.IGNORECASE,
    )
    if intel_match:
        tier = intel_match.group("tier").lower()
        model = intel_match.group("model").upper()
        return f"{tier}-{model}"

    ryzen_match = re.search(
        r"\bRyzen\s*(?P<tier>[3579])\s*(?P<model>\d{4,5}[A-Za-z0-9]{0,4})\b",
        combined,
        flags=re.IGNORECASE,
    )
    if ryzen_match:
        tier = ryzen_match.group("tier")
        model = ryzen_match.group("model").upper()
        return f"Ryzen {tier} {model}"

    xeon_match = re.search(
        r"\bXeon(?:\s+(?P<model>[A-Za-z0-9-]+))?\b",
        combined,
        flags=re.IGNORECASE,
    )
    if xeon_match:
        model = xeon_match.group("model")
        return f"Xeon {model.upper()}".strip()

    loose_match = re.search(r"\b(Ryzen\s?[3579]|i[3579])\b", combined, flags=re.IGNORECASE)
    if loose_match:
        return loose_match.group(1).replace("  ", " ")

    return "not listed"


def _extract_ram(text: str) -> str:
    """
    Extract RAM capacity and DDR generation when possible.

    :param text: Listing text.
    :returns: Canonical RAM string or 'not listed'.
    """
    segments = _candidate_segments(text)
    ddr_global_match = re.search(r"\b(DDR[1-5])\b", text, flags=re.IGNORECASE)
    ddr_global = ddr_global_match.group(1).upper() if ddr_global_match else None

    for segment in segments:
        kit_match = re.search(
            r"\b(?P<count>\d+)\s*[xX]\s*(?P<size>\d+)\s*GB\b", segment, flags=re.IGNORECASE
        )
        if kit_match:
            total_gb = int(kit_match.group("count")) * int(kit_match.group("size"))
            ddr_match = re.search(r"\b(DDR[1-5])\b", segment, flags=re.IGNORECASE)
            ddr = ddr_match.group(1).upper() if ddr_match else ddr_global
            if ddr:
                return f"{total_gb} GB {ddr}"
            return f"{total_gb} GB RAM"

        capacity_then_ddr = re.search(
            r"\b(?P<size>128|96|64|48|32|24|16|12|8|4|2)\s*GB\b"
            r"(?:\s*(?:RAM|memory))?(?:\s*[-@+/.a-z0-9]{0,20})?"
            r"\b(?P<ddr>DDR[1-5])\b",
            segment,
            flags=re.IGNORECASE,
        )
        if capacity_then_ddr:
            size = capacity_then_ddr.group("size")
            ddr = capacity_then_ddr.group("ddr").upper()
            return f"{size} GB {ddr}"

        ddr_then_capacity = re.search(
            r"\b(?P<ddr>DDR[1-5])\b(?:\s*[-@+/.a-z0-9]{0,20})?"
            r"\b(?P<size>128|96|64|48|32|24|16|12|8|4|2)\s*GB\b",
            segment,
            flags=re.IGNORECASE,
        )
        if ddr_then_capacity:
            size = ddr_then_capacity.group("size")
            ddr = ddr_then_capacity.group("ddr").upper()
            return f"{size} GB {ddr}"

        generic_ram = re.search(
            r"\b(?P<size>128|96|64|48|32|24|16|12|8|4|2)\s*GB\b(?:\s*(?:RAM|memory))\b",
            segment,
            flags=re.IGNORECASE,
        )
        if generic_ram:
            return f"{generic_ram.group('size')} GB RAM"

    if ddr_global:
        return ddr_global

    return "not listed"


def _extract_storage(text: str) -> str:
    """
    Extract and normalize primary storage text.

    :param text: Listing text.
    :returns: Canonical storage string or 'not listed'.
    """
    for segment in _candidate_segments(text):
        storage_match = re.search(
            r"\b(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>TB|GB)\s*(?P<kind>NVMe|SSD|HDD)\b",
            segment,
            flags=re.IGNORECASE,
        )
        if storage_match:
            size = storage_match.group("size")
            unit = storage_match.group("unit").upper()
            kind = storage_match.group("kind").upper()
            return f"{size} {unit} {kind}"

        nvme_match = re.search(r"\bNVMe\b", segment, flags=re.IGNORECASE)
        if nvme_match:
            return "NVMe"

    return "not listed"


def _extract_psu(text: str) -> str:
    """
    Extract PSU wattage and efficiency rating.

    :param text: Listing text.
    :returns: Canonical PSU string or 'not listed'.
    """
    for segment in _candidate_segments(text):
        watt_match = re.search(r"\b(?P<watt>\d{3,4})\s*W(?:att)?\b", segment, flags=re.IGNORECASE)
        rating_match = re.search(
            r"\b80\+\s*(?P<rating>Bronze|Silver|Gold|Platinum|Titanium)\b",
            segment,
            flags=re.IGNORECASE,
        )

        if watt_match or rating_match:
            parts = []
            if watt_match:
                parts.append(f"{watt_match.group('watt')}W")
            if rating_match:
                parts.append(f"80+ {rating_match.group('rating').title()}")
            return " ".join(parts)

    return "not listed"


def _extract_motherboard(text: str) -> str:
    """
    Extract a motherboard chipset or model.

    :param text: Listing text.
    :returns: Canonical motherboard string or 'not listed'.
    """
    for segment in _candidate_segments(text):
        match = re.search(
            r"\b(?P<board>(?:X|B|A|Z|H)\d{3,4}[A-Z0-9-]*)\b",
            segment,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group("board").upper()

    return "not listed"


def _extract_case(text: str) -> str:
    """
    Extract case information.

    :param text: Listing text.
    :returns: Canonical case string or 'not listed'.
    """
    for segment in _candidate_segments(text):
        if re.search(r"\b(?:mid[-\s]?tower|full[-\s]?tower)\b", segment, flags=re.IGNORECASE):
            return _clean_component_segment(segment)
        if re.search(r"\bcase\b", segment, flags=re.IGNORECASE):
            return _clean_component_segment(segment)

    return "not listed"


def _extract_cpu_cooler(text: str) -> str:
    """
    Extract CPU cooler information.

    :param text: Listing text.
    :returns: Canonical cooler string or 'not listed'.
    """
    for segment in _candidate_segments(text):
        if re.search(
            r"\b(AIO|liquid cooler|air cooler|stock cooler|Noctua|Cooler\s*Master|CoolerMaster)\b",
            segment,
            flags=re.IGNORECASE,
        ):
            cleaned = _clean_component_segment(segment)
            return cleaned.replace("CoolerMaster", "Cooler Master")

    return "not listed"


def _extract_os(text: str) -> str:
    """
    Extract an operating system.

    :param text: Listing text.
    :returns: Canonical OS string or 'not listed'.
    """
    combined = _normalize_spaces(text)

    for pattern in OS_PATTERNS:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            return _normalize_spaces(match.group(0))

    return "not listed"


def _detect_extras(text: str) -> list[str]:
    """
    Detect included extra items from text.

    :param text: Listing text.
    :returns: List of extras.
    """
    found = []

    for item, pattern in EXTRA_PATTERNS.items():
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            preceding_text = text[: match.start()]
            excluded = re.search(
                r"(?:\bno|\bwithout|\bnot included)[^.!?,]*(?:,?\s+or)?\s*$",
                preceding_text,
                flags=re.IGNORECASE,
            )
            if not excluded:
                found.append(item)
                break

    return found


def _detect_flags(text: str) -> list[str]:
    """
    Detect seller/deal signals.

    :param text: Listing text.
    :returns: List of flags.
    """
    text_lower = text.lower()
    flags = []

    for phrase in LOW_KNOWLEDGE_PATTERNS:
        if phrase in text_lower:
            flags.append("low_knowledge_seller")
            break

    if "firm price" in text_lower:
        flags.append("firm_price")

    if re.search(r"\bobo\b", text_lower) or "best offer" in text_lower:
        flags.append("negotiable")

    return flags


def extract_specs(title: str, description: str) -> ParsedSpecs:
    """
    Extract specs from listing title and description.

    :param title: Listing title.
    :param description: Listing description.
    :returns: ParsedSpecs object.
    """
    combined = f"{title}\n{description}"

    return ParsedSpecs(
        gpu=_extract_gpu(combined),
        cpu=_extract_cpu(combined),
        ram=_extract_ram(combined),
        storage=_extract_storage(combined),
        psu=_extract_psu(combined),
        motherboard=_extract_motherboard(combined),
        case=_extract_case(combined),
        cpu_cooler=_extract_cpu_cooler(combined),
        os=_extract_os(combined),
        extras=_detect_extras(combined),
        flags=_detect_flags(combined),
    )
