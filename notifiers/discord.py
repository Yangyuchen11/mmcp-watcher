import httpx
from typing import Dict, Any, Optional
from .base import BaseNotifier

class DiscordNotifier(BaseNotifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.strip()

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        if not self.webhook_url:
            return False

        embed = {
            "title": title,
            "description": content,
            "color": 0x3498db,  # Nice blue
        }
        if url:
            embed["url"] = url

        if extra and "image_url" in extra and extra["image_url"]:
            embed["thumbnail"] = {"url": extra["image_url"]}

        payload = {
            "username": "Mamachapp Bot",
            "embeds": [embed]
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=payload)
                return resp.status_code in (200, 204)
        except Exception as e:
            print(f"[Notifier:Discord] Error: {e}")
            return False
