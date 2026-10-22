import httpx
from typing import Dict, Any, Optional
from .base import BaseNotifier

class ServerChanNotifier(BaseNotifier):
    """Server酱 - 微信推送"""
    def __init__(self, sendkey: str):
        self.sendkey = sendkey.strip()
        self.api_url = f"https://sctapi.ftqq.com/{self.sendkey}.send"

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        if not self.sendkey:
            return False

        desp = content
        if url:
            desp += f"\n\n[点击查看商品详情]({url})"

        payload = {
            "title": title,
            "desp": desp
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.api_url, data=payload)
                return resp.status_code == 200
        except Exception as e:
            print(f"[Notifier:ServerChan] Error: {e}")
            return False
