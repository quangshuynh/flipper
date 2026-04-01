"""
Discord webhook notifier.
"""

import requests
from models import DealEvaluation


def format_deal_message(
    deal: DealEvaluation,
    ai_summary: str | None = None
) -> str:
    """
    Format Discord message for a deal.

    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: Discord message string.
    """
    specs = deal.specs
    extras = ", ".join(specs.extras) if specs.extras else "None"
    flags = ", ".join(specs.flags) if specs.flags else "None"
    distance = (
        f"{deal.distance_miles} miles"
        if deal.distance_miles is not None
        else "unknown"
    )

    message = (
        "🚨 **POTENTIAL DEAL FOUND**\n\n"
        f"**Title:** {deal.listing.title}\n"
        f"**Location:** {deal.listing.location_text or 'not listed'}\n"
        f"**Distance:** {distance}\n\n"
        "**💻 Specs**\n"
        f"GPU: {specs.gpu}\n"
        f"CPU: {specs.cpu}\n"
        f"RAM: {specs.ram}\n"
        f"Storage: {specs.storage}\n"
        f"PSU: {specs.psu}\n"
        f"Motherboard: {specs.motherboard}\n"
        f"Case: {specs.case}\n"
        f"CPU Cooler: {specs.cpu_cooler}\n"
        f"OS: {specs.os}\n\n"
        "**📦 Extras**\n"
        f"{extras}\n\n"
        "**🚩 Flags**\n"
        f"{flags}\n\n"
    )

    if ai_summary:
        message += (
            "**🧠 AI Summary**\n"
            f"{ai_summary}\n\n"
        )

    message += (
        "**💰 Pricing**\n"
        f"Listing Price: ${deal.listing.price:.2f}\n"
        f"Ideal Buy: ${deal.ideal_buy_price:.2f}\n"
        f"Ideal Sell: ${deal.ideal_sell_price:.2f}\n"
        f"Estimated Market Value: ${deal.estimated_value:.2f}\n"
        f"Estimated Profit: ${deal.estimated_profit:.2f}\n"
        f"Deal Score: {deal.score}/100\n\n"
        f"🔗 {deal.listing.url}"
    )

    return message


def send_deal_to_discord(
    webhook_url: str,
    deal: DealEvaluation,
    ai_summary: str | None = None
) -> bool:
    """
    Send a deal alert to Discord.

    :param webhook_url: Discord webhook URL.
    :param deal: DealEvaluation object.
    :param ai_summary: Optional AI-generated summary.
    :returns: True on success, else False.
    """
    message = format_deal_message(deal, ai_summary=ai_summary)

    response = requests.post(
        webhook_url,
        json={"content": message},
        timeout=10
    )

    return response.status_code in (200, 204)