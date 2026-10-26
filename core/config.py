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
        "priority_interval_seconds": 0.5,
        "rush_mode": False,
        "target_keywords": [],
        "exclude_keywords": [],
        "min_price": 0,          # Minimum price in JPY (0 means no lower limit)
        "max_price": 0,          # Maximum price in JPY (0 means no upper limit)
        "scan_pages": 2,
        "priority_rush_interval_seconds": 0.3,
        "priority_timeout_seconds": 4.0,
        "priority_hedge_enabled": True,
        "priority_hedge_delay": 1.5,
        "priority_hedge_timeout_seconds": 2.0,
        "empty_catalog_threshold": 2,          # 连续无商品页面判定为隐式维护的次数 (0为关闭)
        "enable_empty_catalog_detection": True, # 是否启用连续空页面隐式维护熔断
        "auto_rush_duration_seconds": 60       # 开售自动开启极速模式持续时间 (秒，默认为60秒)
    },
    "order": {
        "auto_buy": True,
        "dry_run": True,         # When True, simulates up to confirmation page without placing live COD order
        "quantity": 1,
        "max_orders": 1,         # Max distinct items to purchase (0 means unlimited / buy all matching items)
        "retry_count": 3,
        "retry_delay_seconds": 0.3,
        "session_retries": 3,       # 保持购物车内部快速重试次数
        "session_retry_delay": 0.15, # 保持购物车内部重试间隔 (秒)
        "worker_retries": 5,        # 跨会话整单重试次数
        "worker_retry_delay": 0.2,  # 跨会话重试等待间隔 (秒)
        "max_concurrent_workers": 3, # Maximum concurrent order threads
        "hedge_requests": True,     # 响应超时开启并发对冲竞速重试
        "hedge_delay": 2.5,         # 对冲重试启动等待阈值 (秒)
        "enable_maintenance_direct_order": True, # 维护结束瞬间直接对重点监控商品触发加购下单
        "maintenance_direct_worker_retries": 60  # 维护结束直接加购最大尝试轮数 (持续尝试直至售罄或买到)
    },
    "buyer": {
        "name": "山田　太郎",
        "name_kana": "ヤマダ　タロウ",
        "email": "buyer_example@gmail.com",
        "tel": "08012345678",
        "postal_code": "1000001",
        "prefecture": "東京都",
        "address": "千代田区千代田1-1",
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
        "secret_token": "",
        "password": "987654321",
        "remember_ips": True,
        "authenticated_ips": []
    }
}

def _load_dotenv_file():
    """Reads key=value pairs from any .env file present in workdir or /app and loads into os.environ"""
    candidates = [
        Path(".env"),
        Path("/app/.env"),
        Path(__file__).resolve().parent.parent / ".env"
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'").strip('"')
                            if k:
                                os.environ[k] = v
                print(f"[Config] Successfully loaded environment variables from {candidate}")
                break
            except Exception as e:
                print(f"[Config] Error reading {candidate}: {e}")

class ConfigManager:
    def __init__(self, config_path: Optional[str] = None):
        _load_dotenv_file()
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
                merged = self._deep_merge(DEFAULT_CONFIG, data)
                return self._apply_env_overrides(merged)
        except Exception as e:
            print(f"[Config] Error reading {self.config_path}: {e}. Using defaults.")
            return self._apply_env_overrides(json.loads(json.dumps(DEFAULT_CONFIG)))

    def _apply_env_overrides(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Applies environment variable overrides if provided (ideal for Dokploy Environment tab)"""
        buyer_keys = {
            "BUYER_NAME": "name",
            "BUYER_NAME_KANA": "name_kana",
            "BUYER_EMAIL": "email",
            "BUYER_TEL": "tel",
            "BUYER_POSTAL_CODE": "postal_code",
            "BUYER_PREFECTURE": "prefecture",
            "BUYER_ADDRESS": "address",
            "BUYER_BIKO": "biko",
            "BUYER_PAYMENT": "payment_method"
        }
        has_override = False
        for env_key, dict_key in buyer_keys.items():
            val = os.environ.get(env_key)
            if val and val.strip():
                config.setdefault("buyer", {})[dict_key] = val.strip()
                has_override = True

        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if webhook and webhook.strip():
            config.setdefault("notifications", {}).setdefault("discord", {})["webhook_url"] = webhook.strip()
            config["notifications"]["discord"]["enabled"] = True
            has_override = True

        for p_key in ("SHOP_MIN_PRICE", "SHOP_MAX_PRICE"):
            p_val = os.environ.get(p_key)
            if p_val and p_val.strip():
                try:
                    cfg_field = "min_price" if p_key == "SHOP_MIN_PRICE" else "max_price"
                    config.setdefault("shop", {})[cfg_field] = int(p_val.strip())
                    has_override = True
                except ValueError:
                    pass

        web_pwd = os.environ.get("WEB_PASSWORD")
        if web_pwd and web_pwd.strip():
            config.setdefault("web", {})["password"] = web_pwd.strip()
            has_override = True

        # If any env override was applied, immediately persist to self.config_path so the web UI reads it
        if has_override:
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                print(f"[Config] Synced environment variables into {self.config_path}")
            except Exception as e:
                print(f"[Config] Note: Could not write env back to {self.config_path}: {e}")

        return config

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
