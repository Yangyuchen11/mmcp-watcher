import os
import json
from typing import Dict, Any, List
from pathlib import Path

DEFAULT_CONFIG: Dict[str, Any] = {
    "shop": {
        "base_url": "https://mamachapptoy.cart.fc2.com",
        "monitor_url": "https://mamachapptoy.cart.fc2.com/?sort=5",
        "interval_seconds": 3.0,
        "rush_interval_seconds": 0.8,
        "rush_mode": False,
        "target_keywords": [],  # Empty means monitor all new / restocked items
        "exclude_keywords": [],
        "max_pages": 1
    },
    "order": {
        "auto_buy": True,
        "dry_run": True,         # When True, simulates up to confirmation page without placing live COD order
        "quantity": 1,
        "max_orders": 1,         # Auto-pause after successfully placing N orders
        "retry_count": 3,
        "retry_delay_seconds": 0.5
    },
    "buyer": {
        "name": "楊　雨晨",
        "name_kana": "ヨウウシン",
        "email": "yyc285184831@gmail.com",
        "tel": "08037304421",
        "postal_code": "3510015",
        "prefecture": "埼玉県",
        "address": "朝霞市幸町",
        "biko": "",
        "payment_method": "代金引換"  # Auto-matched or first available option
    },
    "notifications": {
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": ""
        },
        "discord": {
            "enabled": False,
            "webhook_url": ""
        },
        "serverchan": {
            "enabled": False,
            "sendkey": ""
        },
        "bark": {
            "enabled": False,
            "server": "https://api.day.app",
            "key": ""
        },
        "feishu": {
            "enabled": False,
            "webhook_url": ""
        }
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "secret_token": ""
    }
}

class ConfigManager:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = Path(config_path)
        self.config = self.load()

    def load(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            # If config.json doesn't exist, create it with DEFAULT_CONFIG
            self.save_data(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return self._deep_merge(DEFAULT_CONFIG, data)
        except Exception as e:
            print(f"[Config] Error reading {self.config_path}: {e}. Using defaults.")
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def _deep_merge(self, default: Dict[str, Any], custom: Dict[str, Any]) -> Dict[str, Any]:
        merged = json.loads(json.dumps(default))
        for k, v in custom.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = self._deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged

    def save(self) -> bool:
        return self.save_data(self.config)

    def save_data(self, data: Dict[str, Any]) -> bool:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.config = data
            return True
        except Exception as e:
            print(f"[Config] Error saving {self.config_path}: {e}")
            return False

    def update(self, new_data: Dict[str, Any]) -> bool:
        self.config = self._deep_merge(self.config, new_data)
        return self.save()
