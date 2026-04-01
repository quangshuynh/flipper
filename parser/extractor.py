"""
Extract structured specs and signals from listing text.
"""

import re
from models import ParsedSpecs


GPU_PATTERNS = [
    # RTX 5000 series
    r"\bRTX\s?5090\b", r"\bRTX\s?5080\b", r"\bRTX\s?5070(?:\s?Ti)?\b",

    # RTX 4000 series
    r"\bRTX\s?4090\b", r"\bRTX\s?4080(?:\s?Super)?\b",
    r"\bRTX\s?4070(?:\s?Ti|\s?Super)?\b",
    r"\bRTX\s?4060(?:\s?Ti)?\b",

    # RTX 3000
    r"\bRTX\s?3090\b", r"\bRTX\s?3080(?:\s?Ti)?\b",
    r"\bRTX\s?3070(?:\s?Ti)?\b",
    r"\bRTX\s?3060(?:\s?Ti)?\b", r"\bRTX\s?3050\b",

    # GTX 1000
    r"\bGTX\s?1080(?:\s?Ti)?\b", r"\bGTX\s?1070\b",
    r"\bGTX\s?1060\b",
    r"\bGTX\s?1050(?:\s?Ti)?\b",   
    r"\bGTX\s?1030\b",

    # GTX 1600
    r"\bGTX\s?1660(?:\s?Super|\s?Ti)?\b",
    r"\bGTX\s?1650\b",

    # AMD RX
    r"\bRX\s?7900(?:\s?XTX|\s?XT)?\b",
    r"\bRX\s?7800\s?XT\b",
    r"\bRX\s?7700\s?XT\b",
    r"\bRX\s?6800(?:\s?XT)?\b",
    r"\bRX\s?6700\s?XT\b",
    r"\bRX\s?6600(?:\s?XT)?\b",
    r"\bRX\s?580\b", r"\bRX\s?570\b",   

    # Intel Arc 
    r"\bArc\s?A770\b", r"\bArc\s?A750\b", r"\bArc\s?A580\b",
]

GPU_MEMORY_PATTERNS = [
    r"\b\d+\s?GB\s?VRAM\b",
]

CPU_PATTERNS = [
    # Intel
    r"\bi9[-\s]?\d{4,5}[A-Za-z]*\b",
    r"\bi7[-\s]?\d{4,5}[A-Za-z]*\b",
    r"\bi5[-\s]?\d{4,5}[A-Za-z]*\b",
    r"\bi3[-\s]?\d{4,5}[A-Za-z]*\b",

    # AMD Ryzen
    r"\bRyzen\s?9\s?\d{4,5}[A-Za-z]*\b",
    r"\bRyzen\s?7\s?\d{4,5}[A-Za-z]*\b",
    r"\bRyzen\s?5\s?\d{4,5}[A-Za-z]*\b",
    r"\bRyzen\s?3\s?\d{4,5}[A-Za-z]*\b",

    # Loose mentions
    r"\bRyzen\s?[3579]\b", r"\bi[3579]\b",

    # Add Xeon (common resale)
    r"\bXeon\s?[A-Za-z0-9\-]+\b",
]

RAM_PATTERNS = [
    # Capacity
    r"\b(128\s?GB\s?RAM)\b", r"\b(96\s?GB\s?RAM)\b",
    r"\b(64\s?GB\s?RAM)\b", r"\b(48\s?GB\s?RAM)\b",
    r"\b(32\s?GB\s?RAM)\b", r"\b(24\s?GB\s?RAM)\b",
    r"\b(16\s?GB\s?RAM)\b", r"\b(12\s?GB\s?RAM)\b",
    r"\b(8\s?GB\s?RAM)\b", r"\b(4\s?GB\s?RAM)\b",

    # Generation (NEW)
    r"\bDDR5\b", r"\bDDR4\b", r"\bDDR3\b", r"\bDDR2\b", r"\bDDR1\b",

    # Combined formats (common listings)
    r"\b\d+\s?GB\s?DDR[1-5]\b",
]

STORAGE_PATTERNS = [
    r"\b\d+\s?TB\s?(?:NVMe|SSD|HDD)\b",
    r"\b\d+\s?GB\s?(?:NVMe|SSD|HDD)\b",
    r"\bNVMe\b",
]

PSU_PATTERNS = [
    r"\b\d{3,4}\s?W(?:att)?\s?PSU\b",
    r"\b\d{3,4}\s?W\b",
    r"\b80\+\s?(?:Bronze|Silver|Gold|Platinum|Titanium)\b", 
]

MOTHERBOARD_PATTERNS = [
    # AMD
    r"\bX670E?\b", r"\bB650\b", r"\bB550\b", r"\bX570\b", r"\bA320\b",

    # Intel modern
    r"\bZ790\b", r"\bZ690\b", r"\bB760\b", r"\bB660\b", r"\bH610\b",
]

CASE_PATTERNS = [
    r"\bmid[-\s]?tower\b", r"\bfull[-\s]?tower\b", r"\bNZXT\b",
    r"\bCorsair case\b", r"\bRGB case\b"
]

COOLER_PATTERNS = [
    r"\bAIO\b", r"\bliquid cooler\b", r"\bair cooler\b", r"\bstock cooler\b",
    r"\bNoctua\b", r"\bCooler Master\b"
]

OS_PATTERNS = [
    r"\bWindows\s?11(?:\s?Pro)?\b", r"\bWindows\s?10(?:\s?Pro)?\b",
    r"\bLinux\b", r"\bUbuntu\b"
]

EXTRA_KEYWORDS = [
    "monitor", "keyboard", "mouse", "mouses", "headset", "speakers",
    "desk", "mic", "webcam", "chair"
]

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


def _search_patterns(text: str, patterns: list[str]) -> str:
    """
    Search text using a list of regex patterns and return first match.

    :param text: Input text to search.
    :param patterns: Regex patterns to try.
    :returns: Matched string or 'not listed'.
    """
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return "not listed"


def _detect_extras(text: str) -> list[str]:
    """
    Detect included extra items from text.

    :param text: Listing text.
    :returns: List of extras.
    """
    text_lower = text.lower()
    found = []

    for item in EXTRA_KEYWORDS:
        if item in text_lower:
            normalized = "mouse" if item == "mouses" else item
            if normalized not in found:
                found.append(normalized)

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

    if "obo" in text_lower or "best offer" in text_lower:
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
        gpu=_search_patterns(combined, GPU_PATTERNS),
        cpu=_search_patterns(combined, CPU_PATTERNS),
        ram=_search_patterns(combined, RAM_PATTERNS),
        storage=_search_patterns(combined, STORAGE_PATTERNS),
        psu=_search_patterns(combined, PSU_PATTERNS),
        motherboard=_search_patterns(combined, MOTHERBOARD_PATTERNS),
        case=_search_patterns(combined, CASE_PATTERNS),
        cpu_cooler=_search_patterns(combined, COOLER_PATTERNS),
        os=_search_patterns(combined, OS_PATTERNS),
        extras=_detect_extras(combined),
        flags=_detect_flags(combined)
    )