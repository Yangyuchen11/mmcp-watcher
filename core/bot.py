import time
import asyncio
import datetime
from typing import Dict, Any, List, Optional
from collections import deque
from .config import ConfigManager
from .monitor import FC2Monitor, ShopItem
from .checkout import FC2Checkout
from notifiers import NotificationDispatcher

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    JST = datetime.timezone(datetime.timedelta(hours=9))

def now_jst() -> datetime.datetime:
    """Returns current datetime in Japan Standard Time (JST, UTC+9)"""
    return datetime.datetime.now(JST)

class BotEngine:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.is_running = False
        
        # Dual scanning task handles
        self.catalog_monitor_task: Optional[asyncio.Task] = None
        self.priority_monitor_task: Optional[asyncio.Task] = None
        self.monitor_task: Optional[asyncio.Task] = None  # Backwards compatibility
        
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
            "last_scan_status": "Idle",
            "is_in_maintenance": False,
            "active_workers_count": 0,
            "upcoming_count": 0
        }

        # Catalog & Upcoming Items storage
        self.recent_items: List[Dict[str, Any]] = []
        self.all_catalog_items: List[Dict[str, Any]] = []
        self.upcoming_items: Dict[str, Dict[str, Any]] = {}   # item_id -> item_dict
        self.priority_item_ids: set = set()                   # item_ids pinned for high-priority monitoring
        self.order_history: List[Dict[str, Any]] = []
        self.logs: deque = deque(maxlen=600)
        self._log_subscribers: List[asyncio.Queue] = []

        # Multi-worker Concurrency & Session Deduplication
        self.purchased_item_ids: set = set()
        self.active_order_tasks: Dict[str, asyncio.Task] = {}       # item_id -> running Task
        self.active_orders_status: Dict[str, Dict[str, Any]] = {}   # item_id -> live status dict
        self.priority_monitor_tasks: Dict[str, asyncio.Task] = {}   # item_id -> isolated direct detail polling Task
        self._order_semaphore: Optional[asyncio.Semaphore] = None
        self.is_in_maintenance: bool = False
        self._wake_event_catalog: Optional[asyncio.Event] = None
        self._wake_event_priority: Optional[asyncio.Event] = None

    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.config

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._order_semaphore is None:
            max_w = int(self.config.get("order", {}).get("max_concurrent_workers", 3))
            self._order_semaphore = asyncio.Semaphore(max(1, max_w))
        return self._order_semaphore

    def log(self, level: str, message: str):
        now_str = now_jst().strftime("%Y-%m-%d %H:%M:%S")
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
        self._order_semaphore = None  # Recreated on demand with updated max_concurrent_workers
        self.interrupt_sleep()
        self.log("INFO", "配置已重新载入并即时生效")

    def reset_session(self):
        """Resets purchased items memory and counters to begin a fresh session"""
        count = len(self.purchased_item_ids)
        self.purchased_item_ids.clear()
        self.stats["total_orders_succeeded"] = 0
        self.stats["total_orders_attempted"] = 0
        self.log("INFO", f"🔄 抢购会话已重置！已清空此前记录的 {count} 款已购商品，新一轮发售可重新抢购")

    async def sync_full_catalog(self) -> int:
        """Background worker that syncs the entire store catalog in sort=5 order"""
        try:
            self.log("INFO", "📦 正在拉取商城全量货架商品列表（按官方最新修改时间排序）...")
            items = await self.monitor.scan_full_catalog(max_pages=25)
            if items:
                catalog_dicts = []
                upcoming_dict = {}
                for it in items:
                    d = it.to_dict()
                    if it.item_id in self.priority_item_ids:
                        d["is_priority"] = True
                    catalog_dicts.append(d)
                    if it.is_upcoming:
                        upcoming_dict[it.item_id] = d

                self.all_catalog_items = catalog_dicts
                self.recent_items = self.all_catalog_items
                self.upcoming_items = upcoming_dict
                self.stats["upcoming_count"] = len(upcoming_dict)

                upcoming_titles = [d['title'][:20] for d in list(upcoming_dict.values())[:3]]
                upcoming_str = f"（已自动锁定 {len(upcoming_dict)} 款【预计上线】新品: {', '.join(upcoming_titles)}...）" if upcoming_dict else ""
                self.log("SUCCESS", f"✅ 商城全量商品已同步完成！共索引 {len(items)} 款商品 {upcoming_str}")
                return len(items)
        except Exception as e:
            self.log("WARN", f"全量商品同步异常: {e}")
        return 0

    async def scan_upcoming_items_now(self) -> List[Dict[str, Any]]:
        """Explicitly scans all pages and returns fresh upcoming items"""
        await self.sync_full_catalog()
        return list(self.upcoming_items.values())

    def toggle_item_priority(self, item_id: str) -> bool:
        """Toggles manual priority pin for an upcoming item"""
        item_id = str(item_id).strip()
        if item_id in self.priority_item_ids:
            self.priority_item_ids.remove(item_id)
            is_priority = False
        else:
            self.priority_item_ids.add(item_id)
            is_priority = True

        if item_id in self.upcoming_items:
            self.upcoming_items[item_id]["is_priority"] = is_priority

        for it in self.all_catalog_items:
            if it.get("item_id") == item_id:
                it["is_priority"] = is_priority

        self.interrupt_sleep()
        state_str = "【优先监控】" if is_priority else "【普通监控】"
        self.log("INFO", f"📌 商品 ID {item_id} 监控优先级已设为: {state_str}")
        return is_priority

    def get_upcoming_items(self) -> List[Dict[str, Any]]:
        """Returns the list of currently registered upcoming items"""
        return list(self.upcoming_items.values())

    def get_active_orders(self) -> List[Dict[str, Any]]:
        """Returns active concurrent order worker statuses"""
        return list(self.active_orders_status.values())

    def cancel_order_worker(self, item_id: str) -> bool:
        """Manually cancels a running order worker"""
        item_id = str(item_id).strip()
        task = self.active_order_tasks.get(item_id)
        if task and not task.done():
            task.cancel()
            self.active_order_tasks.pop(item_id, None)
            self.active_orders_status.pop(item_id, None)
            self.log("WARN", f"🛑 用户已手动取消商品 ID {item_id} 的并发抢购线程")
            return True
        return False

    async def start(self):
        if self.is_running:
            return

        order_cfg = self.config.get("order", {})
        max_orders = order_cfg.get("max_orders", 1)
        if max_orders > 0 and len(self.purchased_item_ids) >= max_orders:
            self.reset_session()

        self.is_running = True
        self.stats["start_time"] = now_jst().strftime("%Y-%m-%d %H:%M:%S")
        self.log("INFO", "🚀 监控机器人已启动！双线程架构运行中：[线程1] 预计上线商品优先毫秒级扫描 | [线程2] 全量货架常态巡检")
        
        # Async trigger full catalog sync on startup to discover all upcoming items
        asyncio.create_task(self.sync_full_catalog())

        # Start dual concurrent scanning loops
        self.catalog_monitor_task = asyncio.create_task(self._catalog_monitor_loop())
        self.priority_monitor_task = asyncio.create_task(self._priority_monitor_loop())
        self.monitor_task = self.catalog_monitor_task

    def interrupt_sleep(self):
        """Immediately wakes up both monitor loops from sleep to apply interval or config changes without waiting."""
        if self._wake_event_catalog is not None:
            self._wake_event_catalog.set()
        if self._wake_event_priority is not None:
            self._wake_event_priority.set()

    async def _sleep_interruptible_catalog(self, duration: float):
        if self._wake_event_catalog is None:
            self._wake_event_catalog = asyncio.Event()
        self._wake_event_catalog.clear()
        try:
            await asyncio.wait_for(self._wake_event_catalog.wait(), timeout=max(0.01, duration))
        except asyncio.TimeoutError:
            pass

    async def _sleep_interruptible_priority(self, duration: float):
        if self._wake_event_priority is None:
            self._wake_event_priority = asyncio.Event()
        self._wake_event_priority.clear()
        try:
            await asyncio.wait_for(self._wake_event_priority.wait(), timeout=max(0.01, duration))
        except asyncio.TimeoutError:
            pass

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        self.interrupt_sleep()

        for t in list(self.priority_monitor_tasks.values()):
            if t and not t.done():
                t.cancel()
        self.priority_monitor_tasks.clear()

        for t in [self.catalog_monitor_task, self.priority_monitor_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self.catalog_monitor_task = None
        self.priority_monitor_task = None
        self.monitor_task = None
        self.stats["last_scan_status"] = "Paused"
        self.log("WARN", "⏸️ 监控机器人已暂停")

    async def toggle_rush_mode(self) -> bool:
        rush = not self.config.get("shop", {}).get("rush_mode", False)
        self.config["shop"]["rush_mode"] = rush
        self.config_manager.save()
        self.interrupt_sleep()
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

    def update_intervals(self, interval: Optional[float] = None, rush_interval: Optional[float] = None, priority_interval: Optional[float] = None, priority_rush_interval: Optional[float] = None):
        if "shop" not in self.config:
            self.config["shop"] = {}
        if interval is not None:
            try:
                self.config["shop"]["interval_seconds"] = max(0.2, round(float(interval), 2))
            except Exception:
                pass
        if rush_interval is not None:
            try:
                self.config["shop"]["rush_interval_seconds"] = max(0.1, round(float(rush_interval), 2))
            except Exception:
                pass
        if priority_interval is not None:
            try:
                self.config["shop"]["priority_interval_seconds"] = max(0.1, round(float(priority_interval), 2))
            except Exception:
                pass
        if priority_rush_interval is not None:
            try:
                self.config["shop"]["priority_rush_interval_seconds"] = max(0.1, round(float(priority_rush_interval), 2))
            except Exception:
                pass
        self.config_manager.save()
        self.interrupt_sleep()
        self.log("INFO", f"⏱️ 巡检间隔已更新: 货架日常={self.config['shop'].get('interval_seconds', 3.0)}s, 货架极速={self.config['shop'].get('rush_interval_seconds', 0.8)}s, 优先常态={self.config['shop'].get('priority_interval_seconds', 0.5)}s, 优先极速={self.config['shop'].get('priority_rush_interval_seconds', 0.3)}s")

    # =========================================================================
    # THREAD 1: Priority Scanner Supervisor & Dedicated Per-Product Loops
    # =========================================================================
    async def _priority_monitor_loop(self):
        """
        Supervisor task for priority/upcoming items.
        Spawns and maintains an ISOLATED, INDEPENDENT polling task for EACH priority item's
        direct product detail page. If one item hangs or hits a network error, other items
        continue polling at millisecond speed without any head-of-line blocking!
        """
        while self.is_running:
            # Clean up finished/cancelled priority tasks
            done_ids = [iid for iid, t in self.priority_monitor_tasks.items() if t.done()]
            for iid in done_ids:
                self.priority_monitor_tasks.pop(iid, None)

            # Determine candidates: upcoming items or manually prioritized items that have NOT been bought yet
            candidates: Dict[str, Dict[str, Any]] = {}
            for it in self.upcoming_items.values():
                iid = str(it.get("item_id"))
                if iid not in self.purchased_item_ids and iid not in self.active_order_tasks:
                    candidates[iid] = it

            for pid in self.priority_item_ids:
                if pid not in self.purchased_item_ids and pid not in self.active_order_tasks:
                    if pid not in candidates:
                        candidates[pid] = {
                            "item_id": pid,
                            "title": f"优先监控商品-{pid}",
                            "url": f"{self.monitor.base_url}/ca13/{pid}/"
                        }

            # Spawn independent monitor task for any candidate not yet running
            for iid, cand in candidates.items():
                if iid not in self.priority_monitor_tasks:
                    task = asyncio.create_task(
                        self._monitor_single_priority_item(cand),
                        name=f"PriorityScan-{iid}"
                    )
                    self.priority_monitor_tasks[iid] = task

            # Cancel tasks for any items that are no longer candidates (e.g. bought or cancelled)
            for iid, task in list(self.priority_monitor_tasks.items()):
                if iid not in candidates and not task.done():
                    task.cancel()
                    self.priority_monitor_tasks.pop(iid, None)

            await self._sleep_interruptible_priority(1.0)

    async def _monitor_single_priority_item(self, item_dict: Dict[str, Any]):
        """
        Dedicated independent monitor loop for a SINGLE priority item.
        Polls the product's direct detail page (/ca13/{id}/) without going through the catalog.
        Network hangs or errors on this item NEVER affect any other product!
        """
        item_id = str(item_dict.get("item_id"))
        title = item_dict.get("title", f"商品-{item_id}")
        url = item_dict.get("url") or f"{self.monitor.base_url}/ca13/{item_id}/"

        self.log("INFO", f"🎯 [专属独立监控就绪] 商品 [{title}] (ID: {item_id}) 独立极速监控协程已上线，直连专属详情页探测！")

        while self.is_running:
            if item_id in self.purchased_item_ids or item_id in self.active_order_tasks:
                break

            shop_cfg = self.config.get("shop", {})
            rush_mode = shop_cfg.get("rush_mode", False) or self.is_in_maintenance
            # Use dedicated priority_rush_interval_seconds in rush mode, otherwise priority_interval_seconds
            interval = float(shop_cfg.get("priority_rush_interval_seconds", 0.3)) if rush_mode else float(shop_cfg.get("priority_interval_seconds", 0.5))
            timeout = float(shop_cfg.get("priority_timeout_seconds", 4.0))
            hedge_enabled = bool(shop_cfg.get("priority_hedge_enabled", True))
            hedge_delay = float(shop_cfg.get("priority_hedge_delay", 1.5))
            hedge_timeout = float(shop_cfg.get("priority_hedge_timeout_seconds", 2.0))

            try:
                # Direct check of this single product's detail page with hedged racing and configurable timeout
                target_item = await self.monitor.check_item_detail(
                    url,
                    timeout=timeout,
                    hedge_delay=hedge_delay,
                    hedge_timeout=hedge_timeout,
                    enable_hedge=hedge_enabled
                )
                if target_item:
                    if target_item.title and ("优先监控商品" in title or not title):
                        title = target_item.title

                    if target_item.in_stock:
                        # 🔥 THE MOMENT THIS DOLL BECOMES IN STOCK!
                        self.log("SUCCESS", f"🔥【重磅！预计上线商品正式开售！】[{target_item.title}] (ID: {item_id}) 状态已转为有货！专属监控协程立刻派发独立抢购线程！")
                        self.upcoming_items.pop(item_id, None)
                        self.dispatch_order_worker(target_item, event_type="UPCOMING_DROPPED")
                        break
                    elif target_item.is_upcoming:
                        self.upcoming_items[item_id] = target_item.to_dict()
                    else:
                        # Status is neither upcoming nor in stock (e.g. sold out or temp closed)
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Isolated error for this doll: does not impact other dolls at all!
                pass

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

        self.priority_monitor_tasks.pop(item_id, None)

    # =========================================================================
    # THREAD 2: General Catalog Scanner Loop (货架常态与新上架巡检)
    # =========================================================================
    async def _catalog_monitor_loop(self):
        """
        Continuously scans the catalog to index new products, handle store maintenance,
        and detect restocks across all categories.
        """
        while self.is_running:
            shop_cfg = self.config.get("shop", {})
            monitor_url = shop_cfg.get("monitor_url", f"{self.monitor.base_url}/?sort=5")
            rush_mode = shop_cfg.get("rush_mode", False) or self.is_in_maintenance
            interval = shop_cfg.get("rush_interval_seconds", 0.8) if rush_mode else shop_cfg.get("interval_seconds", 3.0)
            target_keywords = shop_cfg.get("target_keywords", [])
            exclude_keywords = shop_cfg.get("exclude_keywords", [])
            scan_pages = shop_cfg.get("scan_pages", 2)
            min_price = shop_cfg.get("min_price", 0)
            max_price = shop_cfg.get("max_price", 0)

            try:
                t0 = time.perf_counter()
                enable_empty_check = shop_cfg.get("enable_empty_catalog_detection", True)
                empty_threshold = shop_cfg.get("empty_catalog_threshold", 2)

                items, triggers, is_maintenance, maint_reason = await self.monitor.check_once(
                    monitor_url=monitor_url,
                    target_keywords=target_keywords,
                    exclude_keywords=exclude_keywords,
                    scan_pages=scan_pages,
                    min_price=min_price,
                    max_price=max_price,
                    force_instock_trigger=False,
                    enable_empty_catalog_check=enable_empty_check,
                    empty_catalog_threshold=empty_threshold
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                self.stats["total_scans"] += 1
                self.stats["last_scan_time"] = now_jst().strftime("%H:%M:%S")
                self.stats["is_in_maintenance"] = is_maintenance
                self.stats["active_workers_count"] = len(self.active_order_tasks)

                # Handle Maintenance Transition
                if is_maintenance:
                    if not self.is_in_maintenance:
                        self.is_in_maintenance = True
                        rush_int = self.config.get("shop", {}).get("rush_interval_seconds", 0.8)
                        reason_tag = f"【原因: {maint_reason}】" if maint_reason else ""
                        self.log("WARN", f"🚨 【发售前维护侦测】检测到 Mamachapp 商城进入维护中！{reason_tag} 通常几分钟后维护结束即刻上架新品！监控已自动开启最高警戒极速轮询 ({rush_int}s)！")
                        await self.dispatcher.broadcast(
                            title="🚨 【发售前维护预警】Mamachapp 商城进入维护状态！",
                            content=f"店铺已进入维护模式{reason_tag}，这是发售前补库存的明确信号！\n机器人已自动切换为最高警戒极速抢购模式（{rush_int}s 轮询），一旦维护结束开售立即抢购！",
                            url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
                        )
                    self.stats["last_scan_status"] = f"维护中 ({maint_reason or 'Maintenance'}, {elapsed_ms}ms)"
                    await self._sleep_interruptible_catalog(interval)
                    continue
                else:
                    if self.is_in_maintenance:
                        self.is_in_maintenance = False
                        self.log("SUCCESS", "⚡ 【维护结束！新品开售！】商城已恢复访问，正在以毫秒级速度执行全量库存扫描与抢购！")
                        await self.dispatcher.broadcast(
                            title="⚡ 【维护结束！新品开售！】Mamachapp 商城已开售！",
                            content="店铺维护已完成，商城已重新开放！机器人正在毫秒级扫描新上架及补货商品，一有库存即刻下单！",
                            url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
                        )
                        triggers = self.monitor.check_matches(
                            items,
                            target_keywords=target_keywords,
                            exclude_keywords=exclude_keywords,
                            min_price=min_price,
                            max_price=max_price,
                            force_instock_trigger=True
                        )

                # Fast Double-Check: If 1st strike of empty catalog on normal interval, probe again in 1.0s to confirm
                if self.monitor.empty_catalog_consecutive > 0 and not rush_mode:
                    self.log("WARN", f"⚠️ 【空页面异常预警】商城响应正常但未检出任何商品情报 ({self.monitor.empty_catalog_consecutive}/{empty_threshold})，将在 1.0s 后执行快速对冲复检...")
                    self.stats["last_scan_status"] = f"空页面复检中 ({self.monitor.empty_catalog_consecutive}/{empty_threshold})"
                    await self._sleep_interruptible_catalog(1.0)
                    continue

                if items:
                    self.stats["last_scan_status"] = f"OK ({len(items)}件最新商品, {elapsed_ms}ms)"
                    # Update local view and register any discovered upcoming items
                    for it in items:
                        if it.is_upcoming:
                            self.upcoming_items[it.item_id] = it.to_dict()

                    self.stats["upcoming_count"] = len(self.upcoming_items)

                    if not self.all_catalog_items:
                        self.recent_items = [it.to_dict() for it in items]
                    else:
                        top_dict = {it.item_id: it.to_dict() for it in items}
                        for idx, existing in enumerate(self.all_catalog_items):
                            if existing["item_id"] in top_dict:
                                self.all_catalog_items[idx] = top_dict[existing["item_id"]]
                        self.recent_items = self.all_catalog_items
                else:
                    self.stats["last_scan_status"] = f"Empty/Warn ({elapsed_ms}ms)"

                # Handle triggers (Non-blocking dispatch)
                if triggers:
                    self.stats["total_matches"] += len(triggers)
                    for event_type, item in triggers:
                        self._process_trigger_event(event_type, item)

            except Exception as e:
                self.stats["last_scan_status"] = f"Error: {e}"
                self.log("ERROR", f"货架巡检异常: {e}")

            await self._sleep_interruptible_catalog(interval)

    # =========================================================================
    # EVENT DISPATCH & MULTI-THREAD ORDER WORKERS (非阻塞并发抢购池)
    # =========================================================================
    def _process_trigger_event(self, event_type: str, item: ShopItem):
        """Processes a trigger event by sending notifications and dispatching order workers"""
        event_name_map = {
            "UPCOMING_DROPPED": "🔥 预计上线商品正式开售！毫秒级优先抢购！",
            "NEW_IN_STOCK": "🔥 发现新商品上架且有货！",
            "RESTOCKED": "⚡ 现有商品补货在库！",
            "NEW_UPCOMING": "📅 发现新增预计上线商品！已加入优先监测队列",
            "NEW_OUT_OF_STOCK": "ℹ️ 发现新上架商品（暂无库存）"
        }
        title = event_name_map.get(event_type, f"商品动态: {event_type}")
        msg = f"商品: {item.title}\n价格: {item.price}\n状态: {item.stock_text}\n商品ID: {item.item_id}"
        
        is_instock_event = event_type in ("UPCOMING_DROPPED", "NEW_IN_STOCK", "RESTOCKED")
        self.log("SUCCESS" if is_instock_event else "INFO", f"{title} -> {item.title} ({item.price})")

        # Async broadcast notification
        asyncio.create_task(self.dispatcher.broadcast(
            title=title,
            content=msg,
            url=item.url,
            extra={"image_url": item.image_url}
        ))

        # Register upcoming item if newly detected as upcoming
        if item.is_upcoming:
            self.upcoming_items[item.item_id] = item.to_dict()
            self.stats["upcoming_count"] = len(self.upcoming_items)

        # Trigger auto-buy for in-stock triggers
        if is_instock_event:
            self.dispatch_order_worker(item, event_type=event_type)

    def dispatch_order_worker(self, item: ShopItem, event_type: str = "IN_STOCK"):
        """
        Dispatches an independent background order worker task for this product.
        Completely non-blocking: returns in milliseconds so the scanner keeps scanning!
        """
        order_cfg = self.config.get("order", {})
        auto_buy = order_cfg.get("auto_buy", True)
        item_id = item.item_id

        if not auto_buy:
            self.log("INFO", f"ℹ️ 自动购买开关为关闭状态，仅执行提醒，不触发自动下单: {item.title}")
            return

        # 1. Deduplication: already successfully purchased?
        if item_id in self.purchased_item_ids:
            self.log("INFO", f"⏩ 商品 [{item.title}] (ID: {item_id}) 此前已成功下单，自动跳过以防同款重复购买")
            return

        # 2. Concurrency: already has an active order worker running for this item?
        if item_id in self.active_order_tasks and not self.active_order_tasks[item_id].done():
            return

        # 3. Quota check: max distinct items to purchase
        max_orders = order_cfg.get("max_orders", 1)
        if max_orders > 0 and len(self.purchased_item_ids) >= max_orders:
            self.log("WARN", f"已达到设定的最大购买款式数 ({len(self.purchased_item_ids)}/{max_orders} 款)，跳过此次抢购: {item.title}")
            return

        # 4. Launch dedicated background task!
        task = asyncio.create_task(self._run_order_worker(item, event_type))
        self.active_order_tasks[item_id] = task
        self.stats["active_workers_count"] = len(self.active_order_tasks)

    async def _run_order_worker(self, item: ShopItem, event_type: str):
        """
        Independent concurrent order worker for a specific product.
        Runs in background, retries on 502/503/server busy, and releases when bought or sold out.
        Does NOT block scanning or other order threads!
        """
        item_id = item.item_id
        order_cfg = self.config.get("order", {})
        session_retries = int(order_cfg.get("session_retries", 3))
        session_retry_delay = float(order_cfg.get("session_retry_delay", 0.15))
        max_worker_retries = int(order_cfg.get("worker_retries", 5))
        retry_delay = float(order_cfg.get("worker_retry_delay", 0.2))
        quantity = int(order_cfg.get("quantity", 1))
        dry_run = bool(order_cfg.get("dry_run", True))
        hedge_enabled = bool(order_cfg.get("hedge_requests", True))
        hedge_delay = float(order_cfg.get("hedge_delay", 2.5))

        self.stats["total_orders_attempted"] += 1
        self.active_orders_status[item_id] = {
            "item_id": item_id,
            "title": item.title,
            "status": "正在连接结算会话...",
            "attempt": 1,
            "max_retries": max_worker_retries,
            "start_time": now_jst().strftime("%H:%M:%S")
        }

        mode_str = "🛡️ [模拟]" if dry_run else "⚠️ [实盘]"
        priority_str = "⚡ [预计上线优先抢购]" if event_type == "UPCOMING_DROPPED" else "🚀 [并发抢购线程]"
        self.log("ORDER", f"{priority_str} 开始为商品 [{item.title}] (ID: {item_id}) 发起毫秒级并发抢购 (独立会话, 保持Cookie重试 {session_retries} 次, 整单轮数 {max_worker_retries}, 对冲竞速 {'开启' if hedge_enabled else '关闭'}, {mode_str})...")

        async def _single_attempt(label: str) -> Dict[str, Any]:
            checkout = FC2Checkout(
                base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
                buyer_info=self.config.get("buyer", {}),
                dry_run=dry_run,
                retry_count=session_retries,
                retry_delay=session_retry_delay
            )
            try:
                return await checkout.execute_checkout(
                    item_id=item_id,
                    item_title=item.title,
                    item_url=item.url,
                    quantity=quantity
                )
            finally:
                await checkout.close()

        sem = self._get_semaphore()
        async with sem:
            try:
                for attempt in range(1, max_worker_retries + 1):
                    self.active_orders_status[item_id]["attempt"] = attempt
                    self.active_orders_status[item_id]["status"] = f"加购/提交中 (轮次 {attempt}/{max_worker_retries})"

                    res: Optional[Dict[str, Any]] = None

                    if not hedge_enabled:
                        res = await _single_attempt("主请求")
                    else:
                        # 🚀 滚动并发对冲机制 (Rolling Hedged Requests):
                        # 启动主请求线程；若超过 hedge_delay 秒未响应，不杀死原主线程（保持原连接排队等待），
                        # 同时启动对冲探针线程竞速。若对冲探针在 hedge_delay 秒内依然无响应（卡在排队），
                        # 自动切断该卡死的对冲探针并重启新探针！一旦任一探针或主线程成功，立即获胜并清理其他线程！
                        t1 = asyncio.create_task(_single_attempt("主请求"))
                        done, pending = await asyncio.wait([t1], timeout=hedge_delay)

                        if t1 in done:
                            try:
                                res = t1.result()
                            except Exception as err:
                                res = {"success": False, "message": f"主请求异常: {err}"}
                        else:
                            self.log("WARN", f"⏱️ 商品 [{item.title}] (ID: {item_id}) 主请求超过 {hedge_delay}s 未响应，维持主连接排队，启动滚动对冲探针竞速！")
                            max_hedge_probes = 4
                            current_hedge_task: Optional[asyncio.Task] = None
                            winner_res: Optional[Dict[str, Any]] = None

                            for probe_idx in range(1, max_hedge_probes + 1):
                                if t1.done():
                                    try:
                                        winner_res = t1.result()
                                    except Exception as err:
                                        winner_res = {"success": False, "message": f"主请求异常: {err}"}
                                    break

                                self.log("INFO", f"⚡ 启动第 {probe_idx} 轮对冲探针冲刺（主线程仍在保持排队等待）...")
                                self.active_orders_status[item_id]["status"] = f"滚动对冲中 (探针#{probe_idx}, 主线程排队中)"
                                current_hedge_task = asyncio.create_task(_single_attempt(f"对冲探针#{probe_idx}"))

                                done2, pending2 = await asyncio.wait(
                                    [t1, current_hedge_task],
                                    timeout=hedge_delay,
                                    return_when=asyncio.FIRST_COMPLETED
                                )

                                # Case 1: 主线程完成
                                if t1 in done2:
                                    if current_hedge_task and not current_hedge_task.done():
                                        current_hedge_task.cancel()
                                    try:
                                        winner_res = t1.result()
                                    except Exception as err:
                                        winner_res = {"success": False, "message": f"主请求异常: {err}"}
                                    break

                                # Case 2: 当前对冲探针完成
                                if current_hedge_task in done2:
                                    try:
                                        hedge_res = current_hedge_task.result()
                                    except Exception as err:
                                        hedge_res = {"success": False, "message": f"对冲探针#{probe_idx}异常: {err}"}

                                    if hedge_res.get("success") or hedge_res.get("is_soldout"):
                                        if not t1.done():
                                            t1.cancel()
                                        winner_res = hedge_res
                                        break
                                    else:
                                        self.log("WARN", f"⚠️ 对冲探针#{probe_idx} 未抢到 ({hedge_res.get('message')})，主线程继续保持排队，准备下一轮探针...")
                                        current_hedge_task = None
                                        continue

                                # Case 3: hedge_delay 超时，两者均未完成（对冲探针也卡在排队）
                                if current_hedge_task and not current_hedge_task.done():
                                    self.log("WARN", f"✂️ 对冲探针#{probe_idx} 启动超过 {hedge_delay}s 仍无响应（卡在排队），自动切断并重启新连接！主线程继续保持排队...")
                                    current_hedge_task.cancel()
                                    current_hedge_task = None

                            # 若所有探针尝试完后主线程仍未完，给予主线程最后的宽限等待
                            if not winner_res:
                                active_remaining = [t for t in [t1, current_hedge_task] if t and not t.done()]
                                if active_remaining:
                                    done_final, _ = await asyncio.wait(active_remaining, timeout=3.0, return_when=asyncio.FIRST_COMPLETED)
                                    if done_final:
                                        try:
                                            winner_res = list(done_final)[0].result()
                                        except Exception as err:
                                            winner_res = {"success": False, "message": f"最终等待异常: {err}"}
                                    for t in active_remaining:
                                        if not t.done():
                                            t.cancel()

                            res = winner_res if winner_res else {"success": False, "message": "对冲探针所有轮次均无响应"}

                    # Check order outcome
                    if res.get("success"):
                        self.purchased_item_ids.add(item_id)
                        self.upcoming_items.pop(item_id, None)
                        self.stats["total_orders_succeeded"] = len(self.purchased_item_ids)

                        self.order_history.insert(0, res)
                        if len(self.order_history) > 50:
                            self.order_history.pop()

                        order_title = f"{'🛡️ [模拟]' if res.get('dry_run') else '🎉 [实盘]'} 抢购成功！"
                        order_content = f"商品: {item.title}\n结果: {res.get('message')}\n耗时: {res.get('elapsed_seconds')}s\n"
                        if res.get("order_id"):
                            order_content += f"订单号: {res.get('order_id')}\n"
                        if res.get("timeline"):
                            order_content += "步骤流程:\n" + "\n".join(f"• {t}" for t in res.get("timeline", []))

                        self.log("SUCCESS", f"🎉 [并发线程抢购成功] {item.title} -> {res.get('message')} (耗时 {res.get('elapsed_seconds')}s)")
                        await self.dispatcher.broadcast(order_title, order_content, url=item.url)

                        max_orders = order_cfg.get("max_orders", 1)
                        if max_orders > 0 and len(self.purchased_item_ids) >= max_orders:
                            self.log("SUCCESS", f"🎉 已成功抢齐全部预期款式 ({len(self.purchased_item_ids)}/{max_orders} 款)，自动停止监控！")
                            await self.stop()
                        else:
                            remaining = "不限" if max_orders == 0 else f"还可抢购 {max_orders - len(self.purchased_item_ids)} 款"
                            self.log("INFO", f"已成功抢下 1 款商品 (ID: {item_id})，当前已获 {len(self.purchased_item_ids)} 款，继续监控其余款式 ({remaining})")
                        return

                    # Handle failure: only genuine soldout (在庫切れ / 売り切れ / 品切れ / 完売 / 售罄) stops retrying!
                    # "购物车为空" (カートに商品は入っていません) does NOT stop retrying, worker will continue rapid attempts.
                    is_soldout = res.get("is_soldout", False)
                    if not is_soldout:
                        msg = res.get("message", "")
                        if any(k in msg for k in ["在庫切れ", "売り切れ", "品切れ", "完売", "售罄"]):
                            is_soldout = True

                    if is_soldout:
                        self.log("WARN", f"❌ 商品 [{item.title}] (ID: {item_id}) 已确认售罄 (在庫切れ)，并发下单线程停止抢购")
                        self.order_history.insert(0, res)
                        return

                    # Shop busy / 502 / 503 / network jitter -> Persistent fast retry
                    if attempt < max_worker_retries:
                        self.log("WARN", f"⚡ [并发线程下单繁忙] 商品 [{item.title}] (ID: {item_id}): {res.get('message')}，正在毫秒级重复重试 (第 {attempt + 1}/{max_worker_retries} 次)...")
                        await asyncio.sleep(retry_delay)
                    else:
                        self.log("ERROR", f"❌ 商品 [{item.title}] (ID: {item_id}) 已达最大并发重试次数 ({max_worker_retries} 次)，最终未完成")
                        self.order_history.insert(0, res)

            except Exception as e:
                self.log("ERROR", f"并发下单线程发生异常 [{item.title}]: {e}")
            finally:
                self.active_order_tasks.pop(item_id, None)
                self.active_orders_status.pop(item_id, None)
                self.stats["active_workers_count"] = len(self.active_order_tasks)

    # =========================================================================
    # MANUAL TEST CHECKOUT
    # =========================================================================
    async def manual_test_checkout(self, item_id: str, custom_title: str = "测试商品") -> Dict[str, Any]:
        """Runs an on-demand checkout test (always respects dry_run setting, default True)"""
        self.log("INFO", f"🧪 用户手动发起测试下单: ID={item_id} (模拟模式={self.config.get('order', {}).get('dry_run', True)})")
        checkout = FC2Checkout(
            base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
            buyer_info=self.config.get("buyer", {}),
            dry_run=self.config.get("order", {}).get("dry_run", True),
            retry_count=self.config.get("order", {}).get("retry_count", 3),
            retry_delay=self.config.get("order", {}).get("retry_delay_seconds", 0.3)
        )
        res = await checkout.execute_checkout(
            item_id=item_id,
            item_title=custom_title,
            quantity=1
        )
        self.order_history.insert(0, res)
        self.log("ORDER" if res.get("success") else "WARN", f"手动测试结果: {res.get('message')}")
        return res
