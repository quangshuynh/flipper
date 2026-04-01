"""
Discord webhook notifier.
"""

import requests
from models import DealEvaluation


def build_embed(deal: DealEvaluation, ai_summary: str | None = None) -> dict:
    """
    Build a Discord embed for a deal alert.

    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: Embed payload.
    """
    specs = deal.specs
    extras = ", ".join(specs.extras) if specs.extras else "None"
    flags = ", ".join(specs.flags) if specs.flags else "None"
    distance = (
        f"{deal.distance_miles} miles"
        if deal.distance_miles is not None
        else "unknown"
    )

    description = (
        f"**Location:** {deal.listing.location_text or 'not listed'}\n"
        f"**Distance:** {distance}\n\n"
        f"**GPU:** {specs.gpu}\n"
        f"**CPU:** {specs.cpu}\n"
        f"**RAM:** {specs.ram}\n"
        f"**Storage:** {specs.storage}\n"
        f"**PSU:** {specs.psu}\n"
        f"**Motherboard:** {specs.motherboard}\n"
        f"**Case:** {specs.case}\n"
        f"**CPU Cooler:** {specs.cpu_cooler}\n"
        f"**OS:** {specs.os}\n\n"
        f"**Extras:** {extras}\n"
        f"**Flags:** {flags}\n\n"
    )

    if ai_summary:
        description += f"**AI Summary:** {ai_summary}\n\n"

    description += (
        f"**Listing Price:** ${deal.listing.price:.2f}\n"
        f"**Ideal Buy:** ${deal.ideal_buy_price:.2f}\n"
        f"**Ideal Sell:** ${deal.ideal_sell_price:.2f}\n"
        f"**Estimated Market Value:** ${deal.estimated_value:.2f}\n"
        f"**Estimated Profit:** ${deal.estimated_profit:.2f}\n"
        f"**Deal Score:** {deal.score}/100\n\n"
        f"[Open Listing]({deal.listing.url})"
    )

    return {
        "title": f"🚨 {deal.listing.title}",
        "description": description
    }


def send_deal_to_discord(
    webhook_url: str,
    deal: DealEvaluation,
    ai_summary: str | None = None
) -> bool:
    """
    Send a deal alert to Discord via webhook.

    :param webhook_url: Discord webhook URL.
    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: True on success, else False.
    """
"""
Discord webhook notifier.
"""

import requests
from models import DealEvaluation


def build_embed(deal: DealEvaluation, ai_summary: str | None = None) -> dict:
    """
    Build a Discord embed for a deal alert.

    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: Embed payload.
    """
    specs = deal.specs
    extras = ", ".join(specs.extras) if specs.extras else "None"
    flags = ", ".join(specs.flags) if specs.flags else "None"
    distance = (
        f"{deal.distance_miles:.1f} miles"
        if deal.distance_miles is not None
        else "unknown"
    )

    description = (
        f"**Location:** {deal.listing.location_text or 'not listed'}\n"
        f"**Distance:** {distance}\n\n"
        f"**GPU:** {specs.gpu}\n"
        f"**CPU:** {specs.cpu}\n"
        f"**RAM:** {specs.ram}\n"
        f"**Storage:** {specs.storage}\n"
        f"**PSU:** {specs.psu}\n"
        f"**Motherboard:** {specs.motherboard}\n"
        f"**Case:** {specs.case}\n"
        f"**CPU Cooler:** {specs.cpu_cooler}\n"
        f"**OS:** {specs.os}\n\n"
        f"**Extras:** {extras}\n"
        f"**Flags:** {flags}\n\n"
    )

    if ai_summary:
        description += f"**AI Summary:** {ai_summary}\n\n"

    description += (
        f"**Listing Price:** ${deal.listing.price:.2f}\n"
        f"**Ideal Buy:** ${deal.ideal_buy_price:.2f}\n"
        f"**Ideal Sell:** ${deal.ideal_sell_price:.2f}\n"
        f"**Estimated Market Value:** ${deal.estimated_value:.2f}\n"
        f"**Estimated Profit:** ${deal.estimated_profit:.2f}\n"
        f"**Deal Score:** {deal.score}/100\n\n"
        f"[Open Listing]({deal.listing.url})"
    )

    # Discord limits:
    # title max: 256 chars
    # description max: 4096 chars
    title = f"🚨 {deal.listing.title}"[:256]
    description = description[:4096]

    return {
        "title": title,
        "description": description,
    }


def send_deal_to_discord(
    webhook_url: str,
    deal: DealEvaluation,
    ai_summary: str | None = None
) -> bool:
    """
    Send a deal alert to Discord via webhook.

    :param webhook_url: Discord webhook URL.
    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: True on success, else False.
    """
    webhook_url = webhook_url.strip().replace(
        "https://ptb.discord.com/",
        "https://discord.com/"
    )

    embed = build_embed(deal, ai_summary=ai_summary)

    payload = {
        "embeds": [embed]
    }

    if deal.score >= 90:
        payload["content"] = "@everyone"

    response = requests.post(
        webhook_url,
        params={"wait": "true"},
        json=payload,
        timeout=10
    )

    print("Discord request URL:", response.request.url)
    print("Discord status:", response.status_code)
    print("Discord body:", response.text)

    return response.status_code in (200, 204)