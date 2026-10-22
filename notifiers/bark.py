import httpx
from typing import Dict, Any, Optional
from .base import BaseNotifier

class BarkNotifier(BaseNotifier):
    """Bark iOS Push Notification"""
    def __init__(self, server: str, key: str):
        self.server = server.rstrip("/") if server else "https://api.day.app"
        self.key = key.strip()

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        if not self.key:
            return False

        api_url = f"{self.server}/{self.key}"
        payload = {
            "title": title,
            "body": content,
            "group": "MamachappBot",
            "icon": "https://cart.fc2img.com/favicon.ico"
        }
        if url:
            payload["url"] = url

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(api_url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            print(f"[Notifier:Bark] Error: {e}")
            return False
