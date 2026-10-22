import httpx
from typing import Dict, Any, Optional
from .base import BaseNotifier

class FeishuNotifier(BaseNotifier):
    """飞书 / Lark 自定义机器人 Webhook"""
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.strip()

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        if not self.webhook_url:
            return False

        lines = [f"**{title}**", "", content]
        if url:
            lines.extend(["", f"[👉 点击直达商品链接]({url})"])

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": title
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\n".join(lines)
                        }
                    }
                ]
            }
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            print(f"[Notifier:Feishu] Error: {e}")
            return False
