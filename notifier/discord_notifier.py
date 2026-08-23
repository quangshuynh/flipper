"""Discord webhook notifier."""

import logging

import requests

from models import DealEvaluation

logger = logging.getLogger(__name__)


def build_embed(deal: DealEvaluation, ai_summary: str | None = None) -> dict:
    """
    build a Discord payload without making a network request
    :param deal: evaluated deal
    :param ai_summary: optional AI summary
    :returns: Discord embed payload
    """
    specs = deal.specs
    extras = ", ".join(specs.extras) if specs.extras else "None"
    flags = ", ".join(specs.flags) if specs.flags else "None"
    distance = f"{deal.distance_miles:.1f} miles" if deal.distance_miles is not None else "unknown"
    roi = f"{deal.estimated_roi:.1%}" if deal.estimated_roi is not None else "unavailable"

    description = (
        f"**Location:** {deal.listing.location_text or 'not listed'}\n"
        f"**Distance:** {distance}\n\n"
        f"**GPU:** {specs.gpu}\n**CPU:** {specs.cpu}\n**RAM:** {specs.ram}\n"
        f"**Storage:** {specs.storage}\n**PSU:** {specs.psu}\n"
        f"**Motherboard:** {specs.motherboard}\n**Case:** {specs.case}\n"
        f"**CPU Cooler:** {specs.cpu_cooler}\n**OS:** {specs.os}\n\n"
        f"**Extras:** {extras}\n**Flags:** {flags}\n\n"
    )
    if ai_summary:
        description += f"**AI Summary:** {ai_summary}\n\n"
    description += (
        f"**Listing Price:** ${deal.asking_price:.2f}\n"
        f"**Ideal Buy:** ${deal.ideal_buy_price:.2f}\n"
        f"**Expected Resale:** ${deal.expected_resale_value:.2f}\n"
        f"**Estimated Market Value:** ${deal.estimated_market_value:.2f}\n"
        f"**Gross Profit:** ${deal.estimated_gross_profit:.2f}\n"
        f"**ROI:** {roi}\n**Deal Score:** {deal.score}/100\n\n"
        f"[Open Listing]({deal.listing.url})"
    )

    return {
        "title": f"🚨 {deal.listing.title}"[:256],
        "description": description[:4096],
    }


def send_deal_to_discord(
    webhook_url: str,
    deal: DealEvaluation,
    ai_summary: str | None = None,
) -> bool:
    """
    send a deal alert and return False when Discord is unavailable
    :param webhook_url: Discord webhook URL
    :param deal: evaluated deal
    :param ai_summary: optional AI summary
    :returns: whether Discord accepted the request
    """
    webhook_url = webhook_url.strip().replace("https://ptb.discord.com/", "https://discord.com/")
    payload = {"embeds": [build_embed(deal, ai_summary=ai_summary)]}
    if deal.score >= 90:
        payload["content"] = "@everyone"

    try:
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Discord delivery failed: %s", exc)
        return False

    return response.status_code in (200, 204)
