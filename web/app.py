import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import ConfigManager
from core.bot import BotEngine

BASE_DIR = Path(__file__).resolve().parent

def create_app(config_path: str = "config.json") -> FastAPI:
    app = FastAPI(title="Mamachapp FC2 Bot", version="1.0.0")

    config_manager = ConfigManager(config_path)
    bot = BotEngine(config_manager)

    # Static & templates
    static_dir = BASE_DIR / "static"
    templates_dir = BASE_DIR / "templates"
    static_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(templates_dir))

    @app.on_event("startup")
    async def startup_event():
        # Automatically start monitoring on launch
        await bot.start()

    @app.on_event("shutdown")
    async def shutdown_event():
        await bot.stop()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/api/status")
    async def get_status():
        catalog_display = bot.all_catalog_items if bot.all_catalog_items else bot.recent_items
        return {
            "is_running": bot.is_running,
            "rush_mode": bot.config.get("shop", {}).get("rush_mode", False),
            "dry_run": bot.config.get("order", {}).get("dry_run", True),
            "auto_buy": bot.config.get("order", {}).get("auto_buy", True),
            "stats": bot.stats,
            "recent_items": catalog_display,
            "catalog_total": len(catalog_display),
            "is_in_maintenance": bot.is_in_maintenance,
            "order_history": bot.order_history[:10],
            "config": bot.config
        }

    @app.post("/api/sync-catalog")
    async def sync_catalog():
        count = await bot.sync_full_catalog()
        return {"success": True, "count": count}

    @app.post("/api/start")
    async def start_bot():
        await bot.start()
        return {"success": True, "is_running": True}

    @app.post("/api/stop")
    async def stop_bot():
        await bot.stop()
        return {"success": True, "is_running": False}

    @app.post("/api/toggle-rush")
    async def toggle_rush():
        rush = await bot.toggle_rush_mode()
        return {"success": True, "rush_mode": rush}

    @app.post("/api/toggle-dry-run")
    async def toggle_dry_run():
        dry = await bot.toggle_dry_run()
        return {"success": True, "dry_run": dry}

    @app.post("/api/reset-session")
    async def reset_session():
        bot.reset_session()
        return {"success": True, "message": "Session reset"}

    @app.get("/api/config")
    async def get_config():
        return bot.config

    @app.post("/api/config")
    async def update_config(payload: Dict[str, Any]):
        success = config_manager.update(payload)
        if success:
            bot.reload_config()
            return {"success": True, "config": bot.config}
        else:
            raise HTTPException(status_code=500, detail="Failed to save configuration")

    @app.post("/api/test-checkout")
    async def test_checkout(payload: Dict[str, Any]):
        item_id = str(payload.get("item_id", "")).strip()
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")

        custom_title = payload.get("title", f"测试商品-{item_id}")
        res = await bot.manual_test_checkout(item_id, custom_title)
        return res

    @app.post("/api/test-notify")
    async def test_notify(payload: Optional[Dict[str, Any]] = None):
        webhook_url = ""
        if payload and isinstance(payload, dict):
            webhook_url = str(payload.get("webhook_url", "")).strip()

        if not webhook_url:
            webhook_url = bot.config.get("notifications", {}).get("discord", {}).get("webhook_url", "").strip()

        if not webhook_url:
            raise HTTPException(
                status_code=400,
                detail="未填写 Discord Webhook 地址！请在右侧输入框填写以 https://discord.com/api/webhooks/... 开头的链接"
            )

        from notifiers.discord import DiscordNotifier
        notifier = DiscordNotifier(webhook_url=webhook_url, enabled=True)
        ok, msg = await notifier.send_with_result(
            title="🔔 [连通测试] Mamachapp 监控机器人",
            content="这是一条连通性测试消息，如果您收到此消息，说明 Discord Webhook 配置完全成功！\n- 监控目标: mamachapptoy.cart.fc2.com\n- 排序模式: 官方新着更新順 (sort=5)\n- 发售监控: 已进入高灵敏度就绪状态",
            url=bot.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
        )
        if ok:
            bot.log("SUCCESS", f"Discord Webhook 测试成功发送至: {webhook_url[:35]}...")
            return {"success": True, "message": "✅ 测试消息已成功送达 Discord！请前往频道查收。"}
        else:
            bot.log("ERROR", f"Discord Webhook 测试发送失败: {msg}")
            raise HTTPException(status_code=400, detail=f"Discord 发送失败: {msg}")

    @app.get("/api/logs")
    async def get_logs():
        return list(bot.logs)

    @app.get("/api/logs/stream")
    async def stream_logs():
        q: asyncio.Queue = asyncio.Queue()
        bot.add_log_subscriber(q)

        async def event_generator():
            try:
                # First yield existing logs
                for log_entry in list(bot.logs)[-50:]:
                    yield f"data: {json.dumps(log_entry, ensure_ascii=False)}\n\n"

                # Then stream new logs
                while True:
                    entry = await q.get()
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                bot.remove_log_subscriber(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    return app
