from typing import Dict, Any, Optional
from .discord import DiscordNotifier

class NotificationDispatcher:
    def __init__(self, config: Dict[str, Any]):
        self.notifier: Optional[DiscordNotifier] = None
        self._setup(config)

    def _setup(self, config: Dict[str, Any]):
        dc = config.get("notifications", {}).get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            self.notifier = DiscordNotifier(dc["webhook_url"])

    async def broadcast(self, title: str, content: str, url: str = "", extra: Dict[str, Any] = None):
        if self.notifier:
            await self.notifier.send(title, content, url=url, extra=extra)
