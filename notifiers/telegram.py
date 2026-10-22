import httpx
from typing import Dict, Any, Optional
from .base import BaseNotifier

class TelegramNotifier(BaseNotifier):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        if not self.bot_token or not self.chat_id:
            return False

        text = f"<b>{title}</b>\n\n{content}"
        if url:
            text += f"\n\n🔗 <a href='{url}'>商品直达链接</a>"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.api_url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            print(f"[Notifier:Telegram] Error: {e}")
            return False
