import os
import json
import asyncio
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import ConfigManager
from core.bot import BotEngine

BASE_DIR = Path(__file__).resolve().parent
AUTH_COOKIE_NAME = "mmcp_auth_session"

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

    # Helper: Extract client IP supporting reverse proxies
    def get_client_ip(request: Request) -> str:
        for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
            val = request.headers.get(header)
            if val:
                ip = val.split(",")[0].strip()
                if ip:
                    return ip
        if request.client and request.client.host:
            return request.client.host
        return "127.0.0.1"

    # Helper: Get current configured password
    def get_password() -> str:
        pwd = bot.config.get("web", {}).get("password")
        if pwd is not None and str(pwd).strip():
            return str(pwd).strip()
        return "987654321"

    # Helper: Session token calculation
    def get_session_token(password: str) -> str:
        secret = bot.config.get("web", {}).get("secret_token") or "mmcp_auth_salt_fc2"
        return hashlib.sha256(f"{password}:{secret}".encode("utf-8")).hexdigest()

    # Helper: Check if request is authenticated (by remembered IP or session cookie)
    def is_authenticated(request: Request) -> bool:
        client_ip = get_client_ip(request)
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if not isinstance(auth_ips, list):
            auth_ips = []

        # 1. IP Whitelist check (only if explicitly recorded in auth_ips)
        if client_ip in auth_ips:
            return True

        # 2. Session cookie check
        expected_token = get_session_token(get_password())
        cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
        if cookie_token and cookie_token == expected_token:
            # Auto-register client IP if remember_ips is enabled
            if bot.config.get("web", {}).get("remember_ips", True) and client_ip not in auth_ips:
                auth_ips.append(client_ip)
                bot.config.setdefault("web", {})["authenticated_ips"] = auth_ips
                config_manager.save()
            return True

        return False

    def sanitize_config_for_display(cfg: Dict[str, Any]) -> Dict[str, Any]:
        cfg_copy = json.loads(json.dumps(cfg))
        if "web" in cfg_copy and "password" in cfg_copy["web"]:
            cfg_copy["web"]["password"] = "******"
        return cfg_copy

    # Authentication Middleware
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        # Allow static files, login page, and public auth APIs
        if path.startswith("/static") or path in ("/login", "/api/login", "/api/auth/status"):
            return await call_next(request)

        if path in ("/favicon.ico", "/robots.txt"):
            return await call_next(request)

        # Check authentication
        if not is_authenticated(request):
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "未授权访问，请输入密码登录", "authenticated": False}
                )
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)

    @app.on_event("startup")
    async def startup_event():
        # Automatically start monitoring on launch
        await bot.start()

    @app.on_event("shutdown")
    async def shutdown_event():
        await bot.stop()

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if is_authenticated(request):
            return RedirectResponse(url="/", status_code=303)
        client_ip = get_client_ip(request)
        return templates.TemplateResponse(request=request, name="login.html", context={"client_ip": client_ip})

    @app.post("/api/login")
    async def login_api(request: Request, payload: Dict[str, Any]):
        pwd = str(payload.get("password", "")).strip()
        current_pwd = get_password()
        client_ip = get_client_ip(request)

        if pwd != current_pwd:
            bot.log("WARN", f"⚠️ 控制面板登录认证失败: 密码错误 (来自 IP: {client_ip})")
            return JSONResponse(
                status_code=401,
                content={"success": False, "detail": "访问密码错误，请重新输入"}
            )

        remember_ip = payload.get("remember_ip", True)
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if not isinstance(auth_ips, list):
            auth_ips = []

        if remember_ip and client_ip not in auth_ips:
            auth_ips.append(client_ip)
            bot.config.setdefault("web", {})["authenticated_ips"] = auth_ips
            config_manager.save()
            bot.log("INFO", f"🔐 IP [{client_ip}] 登录成功并已记入永久免密白名单")
        else:
            bot.log("INFO", f"🔐 IP [{client_ip}] 登录成功")

        resp = JSONResponse(content={
            "success": True,
            "message": "登录成功",
            "client_ip": client_ip
        })
        token = get_session_token(current_pwd)
        resp.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            path="/"
        )
        return resp

    @app.post("/api/logout")
    async def logout_api(request: Request):
        client_ip = get_client_ip(request)
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if isinstance(auth_ips, list):
            to_remove = {client_ip}
            if client_ip in ("127.0.0.1", "::1", "localhost"):
                to_remove.update(["127.0.0.1", "::1", "localhost"])
            new_ips = [ip for ip in auth_ips if ip not in to_remove]
            bot.config.setdefault("web", {})["authenticated_ips"] = new_ips
            config_manager.save()
            bot.log("INFO", f"🚪 已解除当前 IP [{client_ip}] 的免密授权并退出登录")

        resp = JSONResponse(content={"success": True, "message": "已退出登录并清除免密状态"})
        resp.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/status")
    async def auth_status_api(request: Request):
        client_ip = get_client_ip(request)
        auth = is_authenticated(request)
        curr_pwd = get_password()
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if not isinstance(auth_ips, list):
            auth_ips = []
        return {
            "authenticated": auth,
            "client_ip": client_ip,
            "is_ip_remembered": client_ip in auth_ips,
            "password_is_default": (curr_pwd == "987654321"),
            "authenticated_ips": auth_ips if auth else []
        }

    @app.post("/api/auth/update-password")
    async def update_password_api(request: Request, payload: Dict[str, Any]):
        new_pwd = str(payload.get("new_password", "")).strip()
        if not new_pwd:
            raise HTTPException(status_code=400, detail="新密码不能为空")
        if len(new_pwd) < 4:
            raise HTTPException(status_code=400, detail="密码长度至少需要 4 位字符")

        current_pwd = get_password()
        old_pwd = payload.get("old_password")
        if old_pwd is not None:
            old_pwd_str = str(old_pwd).strip()
            if old_pwd_str and old_pwd_str != current_pwd:
                raise HTTPException(status_code=400, detail="原密码输入不正确")

        # Update and save to config.json
        bot.config.setdefault("web", {})["password"] = new_pwd
        saved = config_manager.save()
        if not saved:
            raise HTTPException(status_code=500, detail="保存密码至配置文件失败")

        client_ip = get_client_ip(request)
        bot.log("INFO", f"🔑 控制面板登录密码已修改并永久保存 (操作来自 IP: {client_ip})")

        resp = JSONResponse(content={
            "success": True,
            "message": "密码修改成功并已永久保存至配置文件"
        })
        token = get_session_token(new_pwd)
        resp.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax"
        )
        return resp

    @app.post("/api/auth/ips/remove")
    async def remove_ip_api(request: Request, payload: Dict[str, Any]):
        ip = str(payload.get("ip", "")).strip()
        if not ip:
            raise HTTPException(status_code=400, detail="ip is required")
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if isinstance(auth_ips, list) and ip in auth_ips:
            auth_ips.remove(ip)
            bot.config.setdefault("web", {})["authenticated_ips"] = auth_ips
            config_manager.save()
            bot.log("INFO", f"🗑️ 已从免密白名单中移除 IP: {ip}")
        return {"success": True, "authenticated_ips": auth_ips}

    @app.post("/api/auth/ips/add")
    async def add_ip_api(request: Request, payload: Dict[str, Any]):
        ip = str(payload.get("ip", "")).strip()
        if not ip:
            raise HTTPException(status_code=400, detail="ip is required")
        auth_ips = bot.config.get("web", {}).get("authenticated_ips", [])
        if not isinstance(auth_ips, list):
            auth_ips = ["127.0.0.1", "::1"]
        if ip not in auth_ips:
            auth_ips.append(ip)
            bot.config.setdefault("web", {})["authenticated_ips"] = auth_ips
            config_manager.save()
            bot.log("INFO", f"➕ 已将 IP 添加至免密白名单: {ip}")
        return {"success": True, "authenticated_ips": auth_ips}

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
            "upcoming_items": bot.get_upcoming_items(),
            "upcoming_count": len(bot.upcoming_items),
            "active_orders": bot.get_active_orders(),
            "priority_item_ids": list(bot.priority_item_ids),
            "is_in_maintenance": bot.is_in_maintenance,
            "auto_rush_active": getattr(bot, "auto_rush_active", False),
            "order_history": bot.order_history[:10],
            "config": sanitize_config_for_display(bot.config)
        }

    @app.post("/api/sync-catalog")
    async def sync_catalog():
        count = await bot.sync_full_catalog()
        return {"success": True, "count": count, "upcoming_count": len(bot.upcoming_items)}

    @app.get("/api/upcoming")
    async def get_upcoming():
        return {
            "success": True,
            "items": bot.get_upcoming_items(),
            "count": len(bot.upcoming_items)
        }

    @app.post("/api/upcoming/scan")
    async def scan_upcoming():
        items = await bot.scan_upcoming_items_now()
        return {
            "success": True,
            "items": items,
            "count": len(items)
        }

    @app.post("/api/upcoming/priority")
    async def toggle_priority(payload: Dict[str, Any]):
        item_id = str(payload.get("item_id", "")).strip()
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        is_priority = bot.toggle_item_priority(item_id)
        return {"success": True, "item_id": item_id, "is_priority": is_priority}

    @app.get("/api/orders/active")
    async def get_active_orders():
        return {
            "success": True,
            "active_orders": bot.get_active_orders()
        }

    @app.post("/api/orders/cancel")
    async def cancel_order(payload: Dict[str, Any]):
        item_id = str(payload.get("item_id", "")).strip()
        if not item_id:
            raise HTTPException(status_code=400, detail="item_id is required")
        ok = bot.cancel_order_worker(item_id)
        return {"success": ok, "item_id": item_id}

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

    @app.post("/api/update-intervals")
    async def update_intervals(payload: Dict[str, Any]):
        interval = payload.get("interval_seconds")
        rush_interval = payload.get("rush_interval_seconds")
        priority_interval = payload.get("priority_interval_seconds")
        priority_rush_interval = payload.get("priority_rush_interval_seconds")
        bot.update_intervals(interval, rush_interval, priority_interval, priority_rush_interval)
        return {
            "success": True,
            "interval_seconds": bot.config.get("shop", {}).get("interval_seconds", 3.0),
            "rush_interval_seconds": bot.config.get("shop", {}).get("rush_interval_seconds", 0.8),
            "priority_interval_seconds": bot.config.get("shop", {}).get("priority_interval_seconds", 0.5),
            "priority_rush_interval_seconds": bot.config.get("shop", {}).get("priority_rush_interval_seconds", 0.3)
        }

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
        return sanitize_config_for_display(bot.config)

    @app.post("/api/config")
    async def update_config(payload: Dict[str, Any]):
        if "web" in payload:
            submitted_pwd = payload["web"].get("password")
            if submitted_pwd == "******" or not submitted_pwd:
                payload["web"]["password"] = bot.config.get("web", {}).get("password", "987654321")
            if "authenticated_ips" not in payload["web"] and "authenticated_ips" in bot.config.get("web", {}):
                payload["web"]["authenticated_ips"] = bot.config["web"]["authenticated_ips"]

        success = config_manager.update(payload)
        if success:
            bot.reload_config()
            return {"success": True, "config": sanitize_config_for_display(bot.config)}
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
        try:
            webhook_url = ""
            if payload and isinstance(payload, dict):
                webhook_url = str(payload.get("webhook_url", "")).strip()

            if not webhook_url:
                webhook_url = bot.config.get("notifications", {}).get("discord", {}).get("webhook_url", "").strip()

            if not webhook_url:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "detail": "未填写 Discord Webhook 地址！请在右侧输入框填写以 https://discord.com/api/webhooks/... 开头的链接"}
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
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "detail": f"Discord 发送失败: {msg}"}
                )
        except Exception as e:
            bot.log("ERROR", f"测试通知发生内部异常: {e}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "detail": f"内部异常: {str(e)}"}
            )

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
