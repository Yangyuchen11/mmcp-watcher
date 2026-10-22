from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseNotifier(ABC):
    @abstractmethod
    async def send(self, title: str, content: str, url: str = "", extra: Dict[str, Any] = None) -> bool:
        pass
