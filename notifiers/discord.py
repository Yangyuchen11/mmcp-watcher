import httpx
from typing import Dict, Any, Optional, Tuple
from .base import BaseNotifier

class DiscordNotifier(BaseNotifier):
    def __init__(self, webhook_url: str = "", enabled: bool = True, **kwargs):
        self.webhook_url = (webhook_url or "").strip()
        self.enabled = enabled

    async def send(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> bool:
        ok, _ = await self.send_with_result(title, content, url, extra)
        return ok

    async def send_with_result(self, title: str, content: str, url: str = "", extra: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        if not self.webhook_url:
            return False, "未设置 Webhook URL，请在控制台填写"

        url_str = self.webhook_url.strip()
        if not (url_str.startswith("https://discord.com/api/webhooks/") or url_str.startswith("https://discordapp.com/api/webhooks/")):
            return False, "Webhook URL 格式无效，必须以 https://discord.com/api/webhooks/ 开头"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        embed = {
            "title": title,
            "description": content,
            "color": 0x3498db,
            "footer": {"text": "Mamachapp Watcher Bot"}
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
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.post(url_str, json=payload)
                if resp.status_code in (200, 204):
                    return True, "发送成功！请检查您的 Discord 频道"
                else:
                    err_msg = f"Discord 报错 HTTP {resp.status_code}: {resp.text}"
                    print(f"[Notifier:Discord] {err_msg}")
                    return False, err_msg
        except Exception as e:
            err_msg = f"发送网络异常: {e}"
            print(f"[Notifier:Discord] {err_msg}")
            return False, err_msg
