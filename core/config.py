import os
import json
import shutil
from typing import Dict, Any, List, Optional
from pathlib import Path

DEFAULT_CONFIG: Dict[str, Any] = {
    "shop": {
        "base_url": "https://mamachapptoy.cart.fc2.com",
        "monitor_url": "https://mamachapptoy.cart.fc2.com/?sort=5",
        "interval_seconds": 3.0,
        "rush_interval_seconds": 0.8,
        "rush_mode": False,
        "target_keywords": [
          "ロングメイド",
          "ひなちゃん",
          "ひよちゃん",
          "ちぃち",
          "なぁな"
        ],
        "exclude_keywords": [],
        "scan_pages": 2
    },
    "order": {
        "auto_buy": True,
        "dry_run": True,         # When True, simulates up to confirmation page without placing live COD order
        "quantity": 1,
        "max_orders": 1,         # Max distinct items to purchase (0 means unlimited / buy all matching items)
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
        "discord": {
            "enabled": False,
            "webhook_url": ""
        }
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8081,
        "secret_token": ""
    }
}

class ConfigManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self.load()

    def _resolve_config_path(self, explicit_path: Optional[str] = None) -> Path:
        # Priority 1: Environment variable CONFIG_PATH
        env_path = os.environ.get("CONFIG_PATH")
        if env_path:
            return Path(env_path)

        # Priority 2: Explicit parameter passed
        if explicit_path:
            return Path(explicit_path)

        # Priority 3: Check if persistent data directory exists (/app/data or ./data)
        if Path("/app/data").exists():
            return Path("/app/data/config.json")
        if Path("data").exists():
            return Path("data/config.json")

        return Path("config.json")

    def load(self) -> Dict[str, Any]:
        # If target file doesn't exist, try copying from existing config.json / config.example.json
        if not self.config_path.exists():
            seeded = False
            for fallback in [Path("config.json"), Path("config.example.json")]:
                if fallback.exists() and fallback.resolve() != self.config_path.resolve():
                    try:
                        self.config_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(fallback, self.config_path)
                        print(f"[Config] Seeded persistent config from {fallback} to {self.config_path}")
                        seeded = True
                        break
                    except Exception as e:
                        print(f"[Config] Failed to seed from {fallback}: {e}")

            if not seeded:
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

            # Also mirror to workspace config.json if different and writable
            try:
                local_mirror = Path("config.json")
                if local_mirror.resolve() != self.config_path.resolve():
                    with open(local_mirror, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

            return True
        except Exception as e:
            print(f"[Config] Error saving {self.config_path}: {e}")
            return False

    def update(self, new_data: Dict[str, Any]) -> bool:
        self.config = self._deep_merge(self.config, new_data)
        return self.save()
