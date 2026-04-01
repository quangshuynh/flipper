"""
Optional AI enrichment layer for Flipper.

This module sits on top of the rule-based extractor and attempts to:
- fill missing specs
- normalize messy values
- detect extras
- detect seller intent / low-knowledge signals
- produce a short reason for why the listing was flagged

It is designed to be OPTIONAL.
If no API key is present, it safely falls back to the base parsed specs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Any

from models import ParsedSpecs


def _clean_text(text: str) -> str:
    """
    Normalize text before sending it to an AI model.

    :param text: Raw listing text.
    :returns: Cleaned text.
    """
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_value(value: Any) -> str:
    """
    Normalize a returned AI field to a safe string.

    :param value: Raw value from AI response.
    :returns: Normalized string.
    """
    if value is None:
        return "not listed"

    value = str(value).strip()
    if not value:
        return "not listed"

    lowered = value.lower()
    if lowered in {"n/a", "none", "unknown", "null", "not sure"}:
        return "not listed"

    return value


def _merge_lists(base: list[str], new_values: list[Any]) -> list[str]:
    """
    Merge two lists while preserving order and uniqueness.

    :param base: Existing values.
    :param new_values: New values.
    :returns: Combined unique list.
    """
    results: list[str] = []
    seen: set[str] = set()

    for item in [*base, *new_values]:
        cleaned = str(item).strip().lower()
        if not cleaned:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            results.append(cleaned)

    return results


def _prefer_base(base_value: str, ai_value: Any) -> str:
    """
    Keep the regex value unless it is missing.

    :param base_value: Current parsed value.
    :param ai_value: AI-suggested value.
    :returns: Final chosen value.
    """
    if base_value != "not listed":
        return base_value
    return _normalize_value(ai_value)


def _build_prompt(title: str, description: str, base_specs: ParsedSpecs) -> str:
    """
    Build the prompt for AI extraction.

    :param title: Listing title.
    :param description: Listing description.
    :param base_specs: Existing rule-based extraction.
    :returns: Prompt string.
    """
    base_json = json.dumps(asdict(base_specs), indent=2)

    return f"""
You are helping evaluate used computer listings for flipping.

Your job:
1. Read the listing.
2. Infer likely specs only when the text strongly supports them.
3. Never invent details.
4. If a part is missing, return "not listed".
5. Detect included extras like monitor, keyboard, mouse, headset, speakers, webcam, mic, desk, chair.
6. Detect seller signals like:
   - low_knowledge_seller
   - negotiable
   - firm_price
7. Return a short "ai_summary" explaining why this listing may or may not be a good deal.

Return ONLY valid JSON with this schema:
{{
  "gpu": "string",
  "cpu": "string",
  "ram": "string",
  "storage": "string",
  "psu": "string",
  "motherboard": "string",
  "case": "string",
  "cpu_cooler": "string",
  "os": "string",
  "extras": ["string"],
  "flags": ["string"],
  "ai_summary": "string"
}}

Rules:
- Use "not listed" for anything not clearly present.
- Keep model names concise.
- Do not add commentary outside JSON.

Listing title:
{title}

Listing description:
{description}

Existing regex extraction:
{base_json}
""".strip()


def _parse_json_response(text: str) -> dict[str, Any]:
    """
    Parse JSON from an AI response.

    :param text: AI response text.
    :returns: Parsed dict.
    """
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    return json.loads(text)


def _call_openai_responses_api(prompt: str, model: str) -> dict[str, Any]:
    """
    Call OpenAI Responses API using the openai Python package.

    Requires:
    - OPENAI_API_KEY
    - openai package installed

    :param prompt: Prompt text.
    :param model: Model name.
    :returns: Parsed JSON dict.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is not installed. Run: pip install openai"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing.")

    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model=model,
        input=prompt,
        temperature=0
    )

    text = getattr(response, "output_text", "").strip()
    if not text:
        raise RuntimeError("AI response was empty.")

    return _parse_json_response(text)


def enrich_specs_with_ai(
    title: str,
    description: str,
    base_specs: ParsedSpecs,
    model: str = "gpt-4.1-mini"
) -> tuple[ParsedSpecs, str]:
    """
    Enrich parsed specs with AI.

    Behavior:
    - If AI is unavailable, returns the base specs unchanged.
    - Keeps regex-extracted values unless missing.
    - Merges extras and flags.
    - Returns a short AI summary.

    :param title: Listing title.
    :param description: Listing description.
    :param base_specs: Existing extracted specs.
    :param model: OpenAI model name.
    :returns: Tuple of (enriched specs, ai summary).
    """
    prompt = _build_prompt(
        title=_clean_text(title),
        description=_clean_text(description),
        base_specs=base_specs
    )

    try:
        result = _call_openai_responses_api(prompt, model=model)
    except Exception:
        return base_specs, "AI enrichment unavailable. Used regex extraction only."

    enriched = ParsedSpecs(
        gpu=_prefer_base(base_specs.gpu, result.get("gpu")),
        cpu=_prefer_base(base_specs.cpu, result.get("cpu")),
        ram=_prefer_base(base_specs.ram, result.get("ram")),
        storage=_prefer_base(base_specs.storage, result.get("storage")),
        psu=_prefer_base(base_specs.psu, result.get("psu")),
        motherboard=_prefer_base(base_specs.motherboard, result.get("motherboard")),
        case=_prefer_base(base_specs.case, result.get("case")),
        cpu_cooler=_prefer_base(base_specs.cpu_cooler, result.get("cpu_cooler")),
        os=_prefer_base(base_specs.os, result.get("os")),
        extras=_merge_lists(base_specs.extras, result.get("extras", [])),
        flags=_merge_lists(base_specs.flags, result.get("flags", [])),
    )

    ai_summary = str(result.get("ai_summary", "")).strip()
    if not ai_summary:
        ai_summary = "AI enrichment completed."

    return enriched, ai_summary