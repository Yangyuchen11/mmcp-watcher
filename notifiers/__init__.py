import asyncio
from typing import Dict, Any, List
from .base import BaseNotifier
from .telegram import TelegramNotifier
from .discord import DiscordNotifier
from .serverchan import ServerChanNotifier
from .bark import BarkNotifier
from .feishu import FeishuNotifier

class NotificationDispatcher:
    def __init__(self, config: Dict[str, Any]):
        self.notifiers: List[BaseNotifier] = []
        self._setup(config)

    def _setup(self, config: Dict[str, Any]):
        notifs = config.get("notifications", {})

        # Telegram
        tg = notifs.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            self.notifiers.append(TelegramNotifier(tg["bot_token"], tg["chat_id"]))

        # Discord
        dc = notifs.get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            self.notifiers.append(DiscordNotifier(dc["webhook_url"]))

        # ServerChan
        sc = notifs.get("serverchan", {})
        if sc.get("enabled") and sc.get("sendkey"):
            self.notifiers.append(ServerChanNotifier(sc["sendkey"]))

        # Bark
        bk = notifs.get("bark", {})
        if bk.get("enabled") and bk.get("key"):
            self.notifiers.append(BarkNotifier(bk.get("server", "https://api.day.app"), bk["key"]))

        # Feishu
        fs = notifs.get("feishu", {})
        if fs.get("enabled") and fs.get("webhook_url"):
            self.notifiers.append(FeishuNotifier(fs["webhook_url"]))

    async def broadcast(self, title: str, content: str, url: str = "", extra: Dict[str, Any] = None):
        if not self.notifiers:
            return

        tasks = [
            n.send(title, content, url=url, extra=extra)
            for n in self.notifiers
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
