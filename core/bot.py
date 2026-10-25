import time
import asyncio
import datetime
from typing import Dict, Any, List, Optional
from collections import deque
from .config import ConfigManager
from .monitor import FC2Monitor, ShopItem, SOLDOUT_KEYWORDS
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
        self.daily_schedule_task: Optional[asyncio.Task] = None
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
            "upcoming_count": 0,
            "prewarmed_count": 0
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
        self.simulated_item_ids: set = set()                        # item_ids simulated successfully in dry_run mode
        self.soldout_item_ids: set = set()                          # item_ids confirmed sold out by detail monitor or checkout
        self.active_order_tasks: Dict[str, asyncio.Task] = {}       # item_id -> running Task
        self.active_orders_status: Dict[str, Dict[str, Any]] = {}   # item_id -> live status dict
        self.priority_monitor_tasks: Dict[str, asyncio.Task] = {}   # item_id -> isolated direct detail polling Task
        self.prewarmed_checkouts: Dict[str, Dict[str, Any]] = {}    # item_id -> {"primary": FC2Checkout, "hedge": FC2Checkout}
        self.prewarm_task: Optional[asyncio.Task] = None
        self._order_semaphore: Optional[asyncio.Semaphore] = None
        self.is_in_maintenance: bool = False
        self.auto_rush_active: bool = False
        self.auto_rush_start_time: float = 0.0
        self.auto_rush_end_time: float = 0.0
        self.auto_rush_has_scanned_after_open: bool = False
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
        self.soldout_item_ids.clear()
        self.stats["total_orders_succeeded"] = 0
        self.stats["total_orders_attempted"] = 0
        self.log("INFO", f"🔄 抢购会话已重置！已清空此前记录的 {count} 款已购商品与售罄状态，新一轮发售可重新抢购")

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

    def _cleanup_order_task(self, item_id: str):
        """Callback to clean up task references upon task completion or cancellation"""
        self.active_order_tasks.pop(item_id, None)
        self.active_orders_status.pop(item_id, None)
        self.stats["active_workers_count"] = len(self.active_order_tasks)

    def cancel_order_worker(self, item_id: str, reason: str = "用户手动取消") -> bool:
        """Cancels a running order worker and cleans up references"""
        item_id = str(item_id).strip()
        task = self.active_order_tasks.get(item_id)
        if task and not task.done():
            task.cancel()
            self._cleanup_order_task(item_id)
            self.log("WARN", f"🛑 [{reason}] 已终止商品 ID {item_id} 的并发抢购线程")
            return True
        elif item_id in self.active_order_tasks:
            self._cleanup_order_task(item_id)
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

        # Start dual concurrent scanning loops, daily 05:00 JST full refresh, and connection pre-warmer
        self.catalog_monitor_task = asyncio.create_task(self._catalog_monitor_loop())
        self.priority_monitor_task = asyncio.create_task(self._priority_monitor_loop())
        self.daily_schedule_task = asyncio.create_task(self._daily_schedule_loop())
        self.prewarm_task = asyncio.create_task(self._prewarm_sessions_loop())
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

        if self.prewarm_task and not self.prewarm_task.done():
            self.prewarm_task.cancel()
            try:
                await self.prewarm_task
            except asyncio.CancelledError:
                pass
        self.prewarm_task = None

        for item_sessions in list(self.prewarmed_checkouts.values()):
            for s in list(item_sessions.values()):
                if s:
                    try:
                        await s.close()
                    except Exception:
                        pass
        self.prewarmed_checkouts.clear()
        self.stats["prewarmed_count"] = 0

        for t in list(self.priority_monitor_tasks.values()):
            if t and not t.done():
                t.cancel()
        self.priority_monitor_tasks.clear()

        for t in [self.catalog_monitor_task, self.priority_monitor_task, self.daily_schedule_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self.catalog_monitor_task = None
        self.priority_monitor_task = None
        self.daily_schedule_task = None
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

            # Determine candidates: upcoming items or manually prioritized items that have NOT been bought yet and NOT confirmed sold out
            candidates: Dict[str, Dict[str, Any]] = {}
            for it in self.upcoming_items.values():
                iid = str(it.get("item_id"))
                if iid not in self.purchased_item_ids and iid not in self.soldout_item_ids:
                    candidates[iid] = it

            for pid in self.priority_item_ids:
                if pid not in self.purchased_item_ids and pid not in self.soldout_item_ids:
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

            # Cancel tasks for any items that are no longer candidates (e.g. bought, sold out, or cancelled)
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
        Runs concurrently alongside independent order workers until bought or sold out.
        """
        item_id = str(item_dict.get("item_id"))
        title = item_dict.get("title", f"商品-{item_id}")
        url = item_dict.get("url") or f"{self.monitor.base_url}/ca13/{item_id}/"

        self.log("INFO", f"🎯 [专属独立监控就绪] 商品 [{title}] (ID: {item_id}) 独立极速监控协程已上线，直连专属详情页探测！")

        while self.is_running:
            if item_id in self.purchased_item_ids or item_id in self.soldout_item_ids:
                break

            shop_cfg = self.config.get("shop", {})
            rush_mode = shop_cfg.get("rush_mode", False) or self.is_in_maintenance or self.auto_rush_active
            # Use dedicated priority_rush_interval_seconds in rush mode, otherwise priority_interval_seconds
            interval = float(shop_cfg.get("priority_rush_interval_seconds", 0.3)) if rush_mode else float(shop_cfg.get("priority_interval_seconds", 0.5))
            timeout = float(shop_cfg.get("priority_timeout_seconds", 4.0))
            hedge_enabled = bool(shop_cfg.get("priority_hedge_enabled", True))
            hedge_delay = float(shop_cfg.get("priority_hedge_delay", 1.5))
            hedge_timeout = float(shop_cfg.get("priority_hedge_timeout_seconds", 2.0))

            try:
                # Direct check of this single product's detail page with hedged racing and configurable timeout
                target_item, is_maint = await self.monitor.check_item_detail(
                    url,
                    timeout=timeout,
                    hedge_delay=hedge_delay,
                    hedge_timeout=hedge_timeout,
                    enable_hedge=hedge_enabled
                )

                # 🛡️ 维护开始前哨交叉联动：若单品详情页率先撞上关店/维护页面，立即将全局同频置为维护状态！
                if is_maint:
                    if not self.is_in_maintenance:
                        self.is_in_maintenance = True
                        self.stats["is_in_maintenance"] = True
                        self.stats["last_scan_status"] = "前哨优先探测：商城维护中 (開店準備中/メンテナンス)"
                        self.log("WARN", f"🚧 【前哨优先侦测】重点商品 [{title}] (ID: {item_id}) 详情页率先检测到店铺进入维护/关店状态！已同步激活全局维护待命状态！")

                if target_item:
                    if target_item.title and ("优先监控商品" in title or not title):
                        title = target_item.title

                    # 🛡️ 维护结束前哨交叉联动：若大盘此前被标记为维护中，此处单品详情页正常刷出为开门铁证！
                    # 立即唤醒大盘解除维护并触发其余重点商品的直接加购冲刺
                    if self.is_in_maintenance:
                        self.is_in_maintenance = False
                        self.stats["is_in_maintenance"] = False
                        self.auto_rush_active = True
                        auto_rush_dur = float(shop_cfg.get("auto_rush_duration_seconds", 60.0))
                        self.auto_rush_start_time = time.time()
                        self.auto_rush_end_time = time.time() + auto_rush_dur
                        self.log("SUCCESS", f"⚡ 【前哨交叉引爆】优先商品 [{title}] (ID: {item_id}) 率先检测到店铺重新开门！已同步解除维护状态并触发全体直接加购！")
                        if self.config.get("order", {}).get("enable_maintenance_direct_order", True):
                            self.trigger_maintenance_direct_orders()

                    if target_item.in_stock:
                        # 🔥 THE MOMENT THIS DOLL BECOMES IN STOCK OR CONTINUES IN STOCK!
                        order_cfg = self.config.get("order", {})
                        dry_run = bool(order_cfg.get("dry_run", False))
                        already_done = (item_id in self.purchased_item_ids) or (dry_run and item_id in self.simulated_item_ids)
                        if not already_done and (item_id not in self.active_order_tasks or self.active_order_tasks[item_id].done()):
                            is_relay = bool(self.upcoming_items.get(item_id, {}).get("in_stock", False))
                            if is_relay:
                                self.log("INFO", f"🔄【在库接力抢购】重点商品 [{target_item.title}] (ID: {item_id}) 依然在库且上一轮已结束，专属协程接力派发全新抢购线程！")
                            else:
                                self.log("SUCCESS", f"🔥【重磅！预计上线商品正式开售！】[{target_item.title}] (ID: {item_id}) 状态已转为有货！专属监控协程立刻派发独立抢购线程！")
                            # 更新其在 upcoming_items 中的在库状态，绝不提前移除！确保专属监控协程与预热通道持续全速运转
                            if item_id in self.upcoming_items:
                                self.upcoming_items[item_id]["in_stock"] = True
                                self.upcoming_items[item_id]["stock_text"] = target_item.stock_text
                            self.dispatch_order_worker(target_item, event_type="UPCOMING_DROPPED")
                    elif target_item.is_upcoming:
                        self.upcoming_items[item_id] = target_item.to_dict()
                    else:
                        # Status is neither upcoming nor in stock (e.g. sold out or temp closed)
                        # Check if confirmed sold out to immediately cancel order attempts and detail polling
                        is_soldout_detail = any(kw in target_item.stock_text for kw in SOLDOUT_KEYWORDS) or not target_item.in_stock
                        if is_soldout_detail:
                            self.soldout_item_ids.add(item_id)
                            self.upcoming_items.pop(item_id, None)
                            self.stats["upcoming_count"] = len(self.upcoming_items)
                            asyncio.create_task(self._close_prewarmed_sessions(item_id))
                            self.log("WARN", f"❌ [专属详情监控] 商品 [{target_item.title}] (ID: {item_id}) 详情页已确认为售罄 ({target_item.stock_text})！")
                            self.cancel_order_worker(item_id, reason="详情页已售罄")
                            break
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
            rush_mode = shop_cfg.get("rush_mode", False) or self.is_in_maintenance or self.auto_rush_active
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
                self.stats["auto_rush_active"] = self.auto_rush_active
                self.stats["active_workers_count"] = len(self.active_order_tasks)

                # Handle Maintenance Transition
                if is_maintenance:
                    if not self.is_in_maintenance:
                        self.is_in_maintenance = True
                        self.auto_rush_active = True
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
                        self.auto_rush_active = True
                        auto_rush_dur = float(shop_cfg.get("auto_rush_duration_seconds", 60.0))
                        self.auto_rush_start_time = time.time()
                        self.auto_rush_end_time = time.time() + auto_rush_dur
                        self.auto_rush_has_scanned_after_open = False
                        self.log("SUCCESS", f"⚡ 【维护结束！新品开售！】商城已恢复访问！自动极速抢购突击已启动（预设持续 {int(auto_rush_dur)}s）！")
                        await self.dispatcher.broadcast(
                            title="⚡ 【维护结束！新品开售！】Mamachapp 商城已开售！",
                            content=f"店铺维护已完成，商城已重新开放！机器人正在以极速突击轮询（预设持续 {int(auto_rush_dur)}s）执行全量库存扫描与抢购！",
                            url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")
                        )

                        # 🔥 维护结束瞬间：直接触发无视页面状态的加购下单线程 (1商品对应1线程)
                        if self.config.get("order", {}).get("enable_maintenance_direct_order", True):
                            self.trigger_maintenance_direct_orders()

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

                # Auto Rush Exit Criteria Check: strictly exits only when configured duration expires
                if self.auto_rush_active and not self.is_in_maintenance:
                    auto_rush_dur = float(shop_cfg.get("auto_rush_duration_seconds", 60.0))
                    if time.time() >= self.auto_rush_end_time:
                        self.auto_rush_active = False
                        self.log("INFO", f"🏁 【极速突击结束】已达到设定的持续时间 ({int(auto_rush_dur)}s)，极速模式自动安全降级为日常巡检。")

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

                # Continuous in-stock replenishment check:
                # If an item on the shelf matches rules and is in_stock, but its previous order worker
                # finished all worker retries without successfully buying, re-dispatch a fresh worker!
                if items and self.config.get("order", {}).get("auto_buy", True):
                    order_cfg = self.config.get("order", {})
                    dry_run = bool(order_cfg.get("dry_run", False))
                    max_orders = order_cfg.get("max_orders", 1)
                    quota_ok = (max_orders <= 0) or (len(self.purchased_item_ids) < max_orders)
                    if quota_ok:
                        for it in items:
                            already_done = (it.item_id in self.purchased_item_ids) or (dry_run and it.item_id in self.simulated_item_ids)
                            if it.in_stock and not already_done and it.item_id not in self.soldout_item_ids:
                                if it.item_id not in self.active_order_tasks or self.active_order_tasks[it.item_id].done():
                                    if self.monitor.matches_rules(it, target_keywords, exclude_keywords, min_price, max_price):
                                        self.log("INFO", f"🔄 货架商品 [{it.title}] (ID: {it.item_id}) 依然有货且尚未购得，重新派发全新独立抢购线程接力！")
                                        self.dispatch_order_worker(it, event_type="IN_STOCK")

            except Exception as e:
                self.stats["last_scan_status"] = f"Error: {e}"
                self.log("ERROR", f"货架巡检异常: {e}")

            await self._sleep_interruptible_catalog(interval)

    # =========================================================================
    # THREAD 3: Daily Scheduled Full Catalog Sync (每日凌晨 05:00 JST 全量同步)
    # =========================================================================
    async def _daily_schedule_loop(self):
        """Triggers a full catalog refresh every day at 05:00 JST"""
        while self.is_running:
            now = now_jst()
            target = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_seconds = max(1.0, (target - now).total_seconds())
            target_str = target.strftime("%Y-%m-%d %H:%M:%S")
            self.log("INFO", f"⏰ [每日定时全量同步就绪] 下一次全量刷新预定于 JST {target_str} (约 {int(wait_seconds / 3600)} 小时后)")
            try:
                await asyncio.sleep(wait_seconds)
                if self.is_running:
                    self.log("INFO", "🌅 [每日定时同步 05:00 JST] 正在执行全商城商品全量索引刷新...")
                    await self.sync_full_catalog()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log("WARN", f"每日定时同步发生异常: {e}")
                await asyncio.sleep(60)

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

        # Async broadcast notification only for:
        # 1. New upcoming product ("NEW_UPCOMING")
        # 2. In-stock / dropped products ("UPCOMING_DROPPED", "NEW_IN_STOCK", "RESTOCKED")
        if event_type in ("UPCOMING_DROPPED", "NEW_IN_STOCK", "RESTOCKED", "NEW_UPCOMING"):
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

    def trigger_maintenance_direct_orders(self):
        """
        Immediately dispatches independent order workers for all upcoming & priority items
        the very millisecond maintenance ends, completely bypassing detail page checks!
        Workers fire POST /cart_in directly and retry aggressively until purchased or sold out.
        """
        order_cfg = self.config.get("order", {})
        if not order_cfg.get("auto_buy", True):
            self.log("INFO", "ℹ️ 自动购买开关为关闭状态，维护结束跳过直接加购触发")
            return

        max_orders = order_cfg.get("max_orders", 1)
        if max_orders > 0 and len(self.purchased_item_ids) >= max_orders:
            self.log("INFO", f"已达成最大购买目标 ({len(self.purchased_item_ids)}/{max_orders} 款)，维护结束跳过直接加购")
            return

        # Collect candidate items:
        # All items currently in upcoming_items or pinned in priority_item_ids
        targets: Dict[str, ShopItem] = {}

        # 1. Known upcoming items
        for iid, it_dict in list(self.upcoming_items.items()):
            iid = str(iid)
            if iid not in self.purchased_item_ids and iid not in self.soldout_item_ids:
                targets[iid] = ShopItem(
                    item_id=iid,
                    title=it_dict.get("title", f"商品-{iid}"),
                    price=it_dict.get("price", "N/A"),
                    url=it_dict.get("url") or f"{self.monitor.base_url}/ca13/{iid}/",
                    image_url=it_dict.get("image_url", ""),
                    in_stock=True,
                    stock_text=it_dict.get("stock_text", "维护结束直发冲刺"),
                    category_id=it_dict.get("category_id")
                )

        # 2. Pinned priority items not already in upcoming_items
        for pid in list(self.priority_item_ids):
            pid = str(pid)
            if pid not in targets and pid not in self.purchased_item_ids and pid not in self.soldout_item_ids:
                targets[pid] = ShopItem(
                    item_id=pid,
                    title=f"重点优先商品-{pid}",
                    price="N/A",
                    url=f"{self.monitor.base_url}/ca13/{pid}/",
                    image_url="",
                    in_stock=True,
                    stock_text="重点优先直发冲刺",
                    category_id="13"
                )

        if not targets:
            self.log("INFO", "ℹ️ 维护结束：当前无待抢的预计上线或重点优先商品，无需直接加购")
            return

        target_ids = list(targets.keys())
        target_names = [it.title[:15] for it in targets.values()]
        self.log("ORDER", f"🚀 ⚡ 【维护结束瞬间直接加购触发】已为 {len(targets)} 款重点商品 (IDs: {target_ids}, 名称: {target_names}) 无视页面状态立即启动独立下单抢购线程！")

        for iid, item in targets.items():
            self.dispatch_order_worker(item, event_type="MAINTENANCE_DIRECT_FIRE")

    def _pop_prewarmed_checkout(self, item_id: str, role: str = "primary") -> Optional[FC2Checkout]:
        """Pops and returns a prewarmed checkout session if available and active"""
        item_id = str(item_id)
        if item_id in self.prewarmed_checkouts:
            checkout = self.prewarmed_checkouts[item_id].pop(role, None)
            if checkout and not checkout.client.is_closed:
                return checkout
        return None

    async def _close_prewarmed_sessions(self, item_id: str):
        """Immediately closes and removes prewarmed TCP/TLS sessions for this item"""
        item_id = str(item_id)
        sessions = self.prewarmed_checkouts.pop(item_id, {})
        for s in sessions.values():
            if s and not s.client.is_closed:
                try:
                    await s.close()
                except Exception:
                    pass
        self.stats["prewarmed_count"] = sum(len(v) for v in self.prewarmed_checkouts.values())

    async def _prewarm_sessions_loop(self):
        """
        Background maintenance loop that maintains pre-warmed TCP+TLS connections
        for candidate products (upcoming or priority items).
        Maintains 2 connections per item:
          - 'primary': for the main order worker
          - 'hedge': for the rolling hedged racing worker
        Pings existing connections with lightweight HEAD every 25 seconds to keep Keep-Alive sockets alive.
        """
        self.log("INFO", "🔥 [双通道会话预热引擎] 已上线！为全部重点商品保持主会话与对冲会话的 TLS 常驻热连接...")

        while self.is_running:
            try:
                # 1. Identify current candidate items
                candidates: Dict[str, Dict[str, Any]] = {}
                for it in self.upcoming_items.values():
                    iid = str(it.get("item_id"))
                    if iid not in self.purchased_item_ids and iid not in self.soldout_item_ids:
                        candidates[iid] = it

                for pid in self.priority_item_ids:
                    pid = str(pid)
                    if pid not in self.purchased_item_ids and pid not in self.soldout_item_ids:
                        if pid not in candidates:
                            candidates[pid] = {
                                "item_id": pid,
                                "title": f"优先监控商品-{pid}",
                                "url": f"{self.monitor.base_url}/ca13/{pid}/"
                            }

                # 2. Clean up prewarmed sessions for items no longer candidates
                for iid in list(self.prewarmed_checkouts.keys()):
                    if iid not in candidates:
                        sessions = self.prewarmed_checkouts.pop(iid, {})
                        for s in sessions.values():
                            if s:
                                try:
                                    await s.close()
                                except Exception:
                                    pass

                # 3. Ensure both 'primary' and 'hedge' connections are active for each candidate
                order_cfg = self.config.get("order", {})
                dry_run = bool(order_cfg.get("dry_run", True))
                session_retries = int(order_cfg.get("session_retries", 3))
                session_retry_delay = float(order_cfg.get("session_retry_delay", 0.15))
                base_url = self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com")

                async def _ensure_warm(iid: str, cand: Dict[str, Any], role: str):
                    if iid not in self.prewarmed_checkouts:
                        self.prewarmed_checkouts[iid] = {}
                    item_sessions = self.prewarmed_checkouts[iid]
                    chk = item_sessions.get(role)
                    needs_new = chk is None or chk.client.is_closed
                    now = time.time()

                    if needs_new:
                        new_chk = FC2Checkout(
                            base_url=base_url,
                            buyer_info=self.config.get("buyer", {}),
                            dry_run=dry_run,
                            retry_count=session_retries,
                            retry_delay=session_retry_delay
                        )
                        warmed = await new_chk.warm_up()
                        if warmed:
                            item_sessions[role] = new_chk
                            role_name = "主会话" if role == "primary" else "对冲会话"
                            title = cand.get("title", f"商品-{iid}")[:15]
                            self.log("INFO", f"♨️ [会话预热就绪] 商品 [{title}] (ID: {iid}) {role_name} 已建立长连接 (TLS 1.3 握手完成，0ms 直发就绪)")
                    else:
                        if now - chk.last_warmed_at > 25.0:
                            ok = await chk.warm_up()
                            if not ok:
                                await chk.close()
                                item_sessions.pop(role, None)

                warm_tasks = [
                    _ensure_warm(iid, cand, role)
                    for iid, cand in candidates.items()
                    for role in ["primary", "hedge"]
                ]
                if warm_tasks:
                    await asyncio.gather(*warm_tasks, return_exceptions=True)

                self.stats["prewarmed_count"] = sum(len(v) for v in self.prewarmed_checkouts.values())

            except asyncio.CancelledError:
                break
            except Exception:
                pass

            await asyncio.sleep(5.0)

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

        dry_run = bool(order_cfg.get("dry_run", False))
        if dry_run and item_id in self.simulated_item_ids:
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
        task.add_done_callback(lambda t: self._cleanup_order_task(item_id))
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
        if event_type == "MAINTENANCE_DIRECT_FIRE":
            # For direct maintenance fire, allow continuous aggressive attempts until sold out or bought
            max_worker_retries = int(order_cfg.get("maintenance_direct_worker_retries", 60))
        retry_delay = float(order_cfg.get("worker_retry_delay", 0.2))
        quantity = int(order_cfg.get("quantity", 1))
        dry_run = bool(order_cfg.get("dry_run", True))
        hedge_enabled = bool(order_cfg.get("hedge_requests", True))
        hedge_delay = float(order_cfg.get("hedge_delay", 2.5))

        self.stats["total_orders_attempted"] += 1
        initial_status = "⚡ 维护结束直发加购中..." if event_type == "MAINTENANCE_DIRECT_FIRE" else "正在连接结算会话..."
        self.active_orders_status[item_id] = {
            "item_id": item_id,
            "title": item.title,
            "status": initial_status,
            "attempt": 1,
            "max_retries": max_worker_retries,
            "start_time": now_jst().strftime("%H:%M:%S")
        }

        mode_str = "🛡️ [模拟]" if dry_run else "⚠️ [实盘]"
        if event_type == "MAINTENANCE_DIRECT_FIRE":
            priority_str = "⚡ [维护结束瞬间直接加购]"
        elif event_type == "UPCOMING_DROPPED":
            priority_str = "⚡ [预计上线优先抢购]"
        else:
            priority_str = "🚀 [并发抢购线程]"
        self.log("ORDER", f"{priority_str} 开始为商品 [{item.title}] (ID: {item_id}) 发起毫秒级并发抢购 (独立会话, 保持Cookie重试 {session_retries} 次, 整单轮数 {max_worker_retries}, 对冲竞速 {'开启' if hedge_enabled else '关闭'}, {mode_str})...")

        # 0. Acquire pre-warmed checkouts if available (zero handshake latency!)
        prewarmed_primary = self._pop_prewarmed_checkout(item_id, "primary")
        prewarmed_hedge = self._pop_prewarmed_checkout(item_id, "hedge")

        async def _single_attempt(label: str, prewarmed_checkout: Optional[FC2Checkout] = None) -> Dict[str, Any]:
            used_prewarm = prewarmed_checkout is not None and not prewarmed_checkout.client.is_closed
            if used_prewarm:
                checkout = prewarmed_checkout
                self.log("ORDER", f"♨️ [{label}] 启用已预热完成的独立 TLS 1.3 长连接通道！0ms握手耗时直接发射加购: [{item.title}] (ID: {item_id})")
            else:
                checkout = FC2Checkout(
                    base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
                    buyer_info=self.config.get("buyer", {}),
                    dry_run=dry_run,
                    retry_count=session_retries,
                    retry_delay=session_retry_delay
                )
            # Ensure runtime config values are bound
            checkout.dry_run = dry_run
            checkout.retry_count = session_retries
            checkout.retry_delay = session_retry_delay
            checkout.buyer_info = self.config.get("buyer", {})

            try:
                return await checkout.execute_checkout(
                    item_id=item_id,
                    item_title=item.title,
                    item_url=item.url,
                    quantity=quantity
                )
            finally:
                await checkout.close()

        try:
            sem = self._get_semaphore()
            async with sem:
                for attempt in range(1, max_worker_retries + 1):
                    # Check if detail page reported sold out
                    if item_id in self.soldout_item_ids:
                        self.log("WARN", f"❌ 商品 [{item.title}] (ID: {item_id}) 页面详情已确认为售罄 (在庫切れ/完売)，下单线程立即终止尝试")
                        return

                    # Check overall quota
                    max_orders = order_cfg.get("max_orders", 1)
                    if max_orders > 0 and len(self.purchased_item_ids) >= max_orders and item_id not in self.purchased_item_ids:
                        self.log("INFO", f"⏹️ 已达到设定的购买目标数 ({len(self.purchased_item_ids)}/{max_orders} 款)，本线程停止对 [{item.title}] 的加购")
                        return

                    self.active_orders_status[item_id]["attempt"] = attempt
                    self.active_orders_status[item_id]["status"] = f"加购/提交中 (轮次 {attempt}/{max_worker_retries})"

                    res: Optional[Dict[str, Any]] = None
                    primary_checkout_arg = prewarmed_primary if attempt == 1 else None

                    if not hedge_enabled:
                        res = await _single_attempt("主请求", primary_checkout_arg)
                    else:
                        # 🚀 滚动并发对冲机制 (Rolling Hedged Requests):
                        # 启动主请求线程；若超过 hedge_delay 秒未响应，不杀死原主线程（保持原连接排队等待），
                        # 同时启动对冲探针线程竞速。若对冲探针在 hedge_delay 秒内依然无响应（卡在排队），
                        # 自动切断该卡死的对冲探针并重启新探针！一旦任一探针或主线程成功，立即获胜并清理其他线程！
                        t1 = asyncio.create_task(_single_attempt("主请求", primary_checkout_arg))
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
                                hedge_checkout_arg = prewarmed_hedge if probe_idx == 1 else None
                                current_hedge_task = asyncio.create_task(_single_attempt(f"对冲探针#{probe_idx}", hedge_checkout_arg))

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
                        is_dry_run = res.get("dry_run", False)
                        if not is_dry_run:
                            # ⚠️ 仅实盘真实抢购成功出单时，才正式标记为已购、从待抢列表中移除并释放长连接！
                            self.purchased_item_ids.add(item_id)
                            self.upcoming_items.pop(item_id, None)
                            self.stats["upcoming_count"] = len(self.upcoming_items)
                            asyncio.create_task(self._close_prewarmed_sessions(item_id))
                            self.stats["total_orders_succeeded"] = len(self.purchased_item_ids)
                        else:
                            # 🛡️ 模拟模式下仅记录模拟出单与流程日志，绝不把商品从监控列表中误剔除，保持长连接热就绪！
                            self.simulated_item_ids.add(item_id)
                            self.log("INFO", f"🛡️ [模拟测试完成] 商品 [{item.title}] (ID: {item_id}) 模拟结算链路跑通，保持预计监控与长连接预热就绪")

                        self.order_history.insert(0, res)
                        if len(self.order_history) > 50:
                            self.order_history.pop()

                        order_title = f"{'🛡️ [模拟]' if is_dry_run else '🎉 [实盘]'} 抢购成功！"
                        order_content = f"商品: {item.title}\n结果: {res.get('message')}\n耗时: {res.get('elapsed_seconds')}s\n"
                        if res.get("order_id"):
                            order_content += f"订单号: {res.get('order_id')}\n"
                        if res.get("timeline"):
                            order_content += "步骤流程:\n" + "\n".join(f"• {t}" for t in res.get("timeline", []))

                        self.log("SUCCESS", f"🎉 [并发线程抢购成功] {item.title} -> {res.get('message')} (耗时 {res.get('elapsed_seconds')}s)")
                        await self.dispatcher.broadcast(order_title, order_content, url=item.url)

                        max_orders = order_cfg.get("max_orders", 1)
                        if not is_dry_run and max_orders > 0 and len(self.purchased_item_ids) >= max_orders:
                            self.log("SUCCESS", f"🎉 已成功抢齐全部预期款式 ({len(self.purchased_item_ids)}/{max_orders} 款)，自动停止其余下单线程与监控！")
                            for other_id, other_task in list(self.active_order_tasks.items()):
                                if other_id != item_id and not other_task.done():
                                    other_task.cancel()
                            await self.stop()
                        elif not is_dry_run:
                            remaining = "不限" if max_orders == 0 else f"还可抢购 {max_orders - len(self.purchased_item_ids)} 款"
                            self.log("INFO", f"已成功抢下 1 款商品 (ID: {item_id})，当前已获 {len(self.purchased_item_ids)} 款，继续监控其余款式 ({remaining})")
                        return

                    # Handle failure: only genuine soldout (在庫切れ / 売り切れ / 品切れ / 完売 / 售罄) stops retrying!
                    # "购物车为空" (カートに商品は入っていません) does NOT stop retrying, worker will continue rapid attempts.
                    is_soldout = res.get("is_soldout", False)
                    if not is_soldout:
                        msg = res.get("message", "")
                        if any(k in msg for k in SOLDOUT_KEYWORDS):
                            is_soldout = True

                    if is_soldout:
                        self.soldout_item_ids.add(item_id)
                        self.upcoming_items.pop(item_id, None)
                        self.stats["upcoming_count"] = len(self.upcoming_items)
                        asyncio.create_task(self._close_prewarmed_sessions(item_id))
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

        except asyncio.CancelledError:
            self.log("INFO", f"⏹️ 商品 [{item.title}] (ID: {item_id}) 抢购线程已停止 (原因: 详情页检测到售罄或任务被取消)")
            raise
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
        order_cfg = self.config.get("order", {})
        dry_run = bool(order_cfg.get("dry_run", True))
        action_name = "模拟测试" if dry_run else "⚡ 实盘真实"
        self.log("INFO", f"🧪 用户手动发起{action_name}下单: ID={item_id} (模拟模式={dry_run})")
        session_retries = int(order_cfg.get("session_retries", order_cfg.get("retry_count", 3)))
        session_retry_delay = float(order_cfg.get("session_retry_delay", order_cfg.get("retry_delay_seconds", 0.15)))
        quantity = int(order_cfg.get("quantity", 1))
        checkout = FC2Checkout(
            base_url=self.config.get("shop", {}).get("base_url", "https://mamachapptoy.cart.fc2.com"),
            buyer_info=self.config.get("buyer", {}),
            dry_run=dry_run,
            retry_count=session_retries,
            retry_delay=session_retry_delay
        )
        try:
            res = await checkout.execute_checkout(
                item_id=item_id,
                item_title=custom_title,
                quantity=quantity
            )
        finally:
            await checkout.close()
        self.order_history.insert(0, res)
        self.log("ORDER" if res.get("success") else "WARN", f"手动{action_name}下单结果: {res.get('message')}")
        return res
