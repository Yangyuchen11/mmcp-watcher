import time
import asyncio
import datetime
from typing import Dict, Any, List, Optional
from collections import deque
from .config import ConfigManager
from .monitor import FC2Monitor, ShopItem
from .checkout import FC2Checkout
from notifiers import NotificationDispatcher

class BotEngine:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        
        self.monitor = FC2Monitor(
            base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
        )
        self.dispatcher = NotificationDispatcher(self.config)

        # In-memory stats
        self.stats = {
            "start_time": None,
            "total_scans": 0,
            "total_matches": 0,
            "total_orders_attempted": 0,
            "total_orders_succeeded": 0,
            "last_scan_time": None,
            "last_scan_status": "Idle"
        }

        self.recent_items: List[Dict[str, Any]] = []
        self.order_history: List[Dict[str, Any]] = []
        self.logs: deque = deque(maxlen=600)
        self._log_subscribers: List[asyncio.Queue] = []

    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.config

    def log(self, level: str, message: str):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "timestamp": now_str,
            "level": level.upper(),
            "message": message
        }
        self.logs.append(entry)
        try:
            print(f"[{now_str}] [{level.upper()}] {message}")
        except Exception:
            try:
                safe_msg = message.encode("ascii", errors="replace").decode("ascii")
                print(f"[{now_str}] [{level.upper()}] {safe_msg}")
            except Exception:
                pass

        # Broadcast to active SSE listeners
        for q in list(self._log_subscribers):
            try:
                q.put_nowait(entry)
            except Exception:
                pass

    def add_log_subscriber(self, q: asyncio.Queue):
        self._log_subscribers.append(q)

    def remove_log_subscriber(self, q: asyncio.Queue):
        if q in self._log_subscribers:
            self._log_subscribers.remove(q)

    def reload_config(self):
        self.dispatcher = NotificationDispatcher(self.config)
        self.monitor.base_url = self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
        self.log("INFO", "配置已重新载入并生效")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.stats["start_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log("INFO", "🚀 监控机器人已启动！开始巡检目标商城...")
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
        self.stats["last_scan_status"] = "Paused"
        self.log("WARN", "⏸️ 监控机器人已暂停")

    async def toggle_rush_mode(self) -> bool:
        rush = not self.config.get("shop", {}).get("rush_mode", False)
        self.config["shop"]["rush_mode"] = rush
        self.config_manager.save()
        status_text = "开启 (极速抢购模式)" if rush else "关闭 (日常巡检模式)"
        self.log("WARN", f"⚡ 极速模式已切换为: {status_text}")
        return rush

    async def toggle_dry_run(self) -> bool:
        dry = not self.config.get("order", {}).get("dry_run", True)
        self.config["order"]["dry_run"] = dry
        self.config_manager.save()
        status_text = "开启 (模拟下单安全模式)" if dry else "关闭 (实盘真实下单模式⚠️)"
        self.log("WARN", f"🛡️ 模拟模式状态: {status_text}")
        return dry

    async def _monitor_loop(self):
        while self.is_running:
            shop_cfg = self.config.get("shop", {})
            order_cfg = self.config.get("order", {})

            monitor_url = shop_cfg.get("monitor_url", "https://mamachapptoy.cart.fc2.com/?sort=5")
            rush_mode = shop_cfg.get("rush_mode", False)
            interval = shop_cfg.get("rush_interval_seconds", 0.8) if rush_mode else shop_cfg.get("interval_seconds", 3.0)
            target_keywords = shop_cfg.get("target_keywords", [])
            exclude_keywords = shop_cfg.get("exclude_keywords", [])

            try:
                t0 = time.perf_counter()
                items, triggers = await self.monitor.check_once(
                    monitor_url=monitor_url,
                    target_keywords=target_keywords,
                    exclude_keywords=exclude_keywords
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                self.stats["total_scans"] += 1
                self.stats["last_scan_time"] = datetime.datetime.now().strftime("%H:%M:%S")

                if items:
                    self.stats["last_scan_status"] = f"OK ({len(items)}件商品, {elapsed_ms}ms)"
                    # Cache latest items for web view
                    self.recent_items = [it.to_dict() for it in items[:30]]
                else:
                    self.stats["last_scan_status"] = f"Empty/Warn ({elapsed_ms}ms)"

                # Handle triggers
                if triggers:
                    self.stats["total_matches"] += len(triggers)
                    for event_type, item in triggers:
                        await self._handle_trigger(event_type, item)

            except Exception as e:
                self.stats["last_scan_status"] = f"Error: {e}"
                self.log("ERROR", f"巡检发生异常: {e}")

            await asyncio.sleep(interval)

    async def _handle_trigger(self, event_type: str, item: ShopItem):
        event_name_map = {
            "NEW_IN_STOCK": "🔥 发现新商品上架且有货！",
            "RESTOCKED": "⚡ 商品补货！",
            "NEW_OUT_OF_STOCK": "ℹ️ 发现新上架商品（暂无库存）"
        }
        title = event_name_map.get(event_type, f"商品动态: {event_type}")
        msg = f"商品: {item.title}\n价格: {item.price}\n状态: {item.stock_text}\n商品ID: {item.item_id}"
        self.log("SUCCESS" if "IN_STOCK" in event_type or "RESTOCKED" in event_type else "INFO", f"{title} -> {item.title} ({item.price})")

        # Send alert
        await self.dispatcher.broadcast(
            title=title,
            content=msg,
            url=item.url,
            extra={"image_url": item.image_url}
        )

        # Trigger auto-buy if eligible
        order_cfg = self.config.get("order", {})
        auto_buy = order_cfg.get("auto_buy", True)
        can_buy = event_type in ("NEW_IN_STOCK", "RESTOCKED")

        if auto_buy and can_buy:
            max_orders = order_cfg.get("max_orders", 1)
            if self.stats["total_orders_succeeded"] >= max_orders:
                self.log("WARN", f"已达到最大允许下单数 ({max_orders})，跳过此次抢购")
                return

            self.log("ORDER", f"⚡ 即刻触发极速下单流水线: {item.title} (ID: {item.item_id})...")
            self.stats["total_orders_attempted"] += 1

            checkout = FC2Checkout(
                base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
                buyer_info=self.config.get("buyer", {}),
                dry_run=order_cfg.get("dry_run", True)
            )

            res = await checkout.execute_checkout(
                item_id=item.item_id,
                item_title=item.title,
                item_url=item.url,
                quantity=order_cfg.get("quantity", 1)
            )

            self.order_history.insert(0, res)
            if len(self.order_history) > 50:
                self.order_history.pop()

            order_title = f"{'🛡️ [模拟]' if res.get('dry_run') else '🎉 [实盘]'} 下单结果: {'成功' if res.get('success') else '未完成'}"
            order_content = f"商品: {item.title}\n结果: {res.get('message')}\n耗时: {res.get('elapsed_seconds')}s\n"
            if res.get("order_id"):
                order_content += f"订单号: {res.get('order_id')}\n"
            if res.get("timeline"):
                order_content += "步骤流程:\n" + "\n".join(f"• {t}" for t in res.get("timeline", []))

            self.log("ORDER" if res.get("success") else "ERROR", f"{order_title}: {res.get('message')}")
            await self.dispatcher.broadcast(order_title, order_content, url=item.url)

            if res.get("success"):
                self.stats["total_orders_succeeded"] += 1
                if self.stats["total_orders_succeeded"] >= max_orders:
                    self.log("SUCCESS", f"已成功完成预期订单数 ({max_orders})，自动暂停监控以防重复下单")
                    await self.stop()

    async def manual_test_checkout(self, item_id: str, custom_title: str = "测试商品") -> Dict[str, Any]:
        """Runs an on-demand checkout test (always respects dry_run setting, default True)"""
        self.log("INFO", f"🧪 用户手动发起测试下单: ID={item_id} (模拟模式={self.config.get('order', {}).get('dry_run', True)})")
        checkout = FC2Checkout(
            base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
            buyer_info=self.config.get("buyer", {}),
            dry_run=self.config.get("order", {}).get("dry_run", True)
        )
        res = await checkout.execute_checkout(
            item_id=item_id,
            item_title=custom_title,
            quantity=1
        )
        self.order_history.insert(0, res)
        self.log("ORDER" if res.get("success") else "WARN", f"手动测试结果: {res.get('message')}")
        return res
