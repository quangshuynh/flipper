import os
import requests
from dotenv import load_dotenv

load_dotenv()
webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
print(webhook_url)

# normalize to the canonical Discord API host
webhook_url = webhook_url.replace("https://ptb.discord.com/", "https://discord.com/")

response = requests.post(
    webhook_url,
    params={"wait": "true"},
    json={"content": "Webhook test from Flipper"},
    timeout=10,
)

print("url   :", response.request.url)
print("status:", response.status_code)
print("body  :", response.text)