import re
import math
import asyncio
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx

try:
    import h2
    HAS_HTTP2 = True
except ImportError:
    HAS_HTTP2 = False

try:
    import lxml
    DEFAULT_PARSER = "lxml"
except ImportError:
    DEFAULT_PARSER = "html.parser"

@dataclass
class ShopItem:
    item_id: str
    title: str
    price: str
    url: str
    image_url: str
    in_stock: bool
    stock_text: str
    category_id: Optional[str] = None
    is_upcoming: bool = False
    scheduled_time: Optional[str] = None
    is_priority: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["price_value"] = self.price_value
        d["status"] = "instock" if self.in_stock else ("upcoming" if self.is_upcoming else "soldout")
        return d

    @property
    def price_value(self) -> Optional[int]:
        """Extracts the integer price value in JPY (e.g. '19,800円' -> 19800)"""
        if not self.price:
            return None
        digits = re.sub(r"[^\d]", "", str(self.price))
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

MAINTENANCE_KEYWORDS = [
    # 核心标准与变体特征词（支持模糊前缀/后缀）
    "メンテナンス中",
    "メンテナンスを行っております",
    "開店準備中",
    "ショップ準備中",
    "準備中",
    "一時閉店",
    "閉店中",
    "休止中",
    "改装中",
    "休業中",
    "カートを閉鎖",
    "カート閉鎖",
    "店舗改装中",
    "販売準備",
    "商品入替中",
]

UPCOMING_KEYWORDS = [
    "開始予定",
    "販売予定",
    "発売予定",
    "販売開始前",
    "予定",
    "coming soon"
]

SOLDOUT_KEYWORDS = [
    "在庫切れ",
    "完売",
    "品切れ",
    "売り切れ",
    "sold out",
    "販売終了"
]

INSTOCK_KEYWORDS = [
    "在庫あり",
    "カートに入れる"
]

class FC2Monitor:
    def __init__(self, base_url: str = "https://mamachapptoy.cart.fc2.com", client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        self.client = client
        self.known_items: Dict[str, ShopItem] = {}
        self.is_initialized = False
        self.empty_catalog_consecutive = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=30, keepalive_expiry=60.0)
            self.client = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(40.0, connect=10.0),
                limits=limits,
                http2=False,  # FC2 Cart CGI OpenResty server stalls/times out on HTTP/2; HTTP/1.1 is significantly faster and reliable
                follow_redirects=True
            )
        return self.client

    async def fetch_page(self, url: str, timeout: Optional[float] = None) -> Tuple[Optional[str], int]:
        client = await self._get_client()
        req_timeout = httpx.Timeout(timeout, connect=min(5.0, timeout if timeout else 5.0)) if timeout else None
        for attempt in range(2):
            try:
                resp = await client.get(url, timeout=req_timeout)
                return resp.text, resp.status_code
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                err_msg = str(e) if str(e) else type(e).__name__
                print(f"[Monitor] Network error fetching {url}: {err_msg}")
                return None, 0
        return None, 0

    async def fetch_page_hedged(
        self,
        url: str,
        timeout: float = 4.0,
        hedge_delay: float = 1.5,
        hedge_timeout: float = 2.0,
        enable_hedge: bool = True
    ) -> Tuple[Optional[str], int]:
        """
        Decoupled Hedged Request for High-Contention FastCGI Queues:
        - Probe 1 (Main Probe) runs with primary `timeout` (e.g. 4.0s-6.0s), holding its place in queue.
        - If no response arrives within `hedge_delay` (e.g. 1.5s), Probe 2 (Hedge Probe) is spawned with its own `hedge_timeout` (e.g. 2.0s).
        - If Probe 2 stalls, only Probe 2 is cut and refreshed with a new connection, WHILE Probe 1 CONTINUES WAITING.
        - Whichever probe finishes first with HTTP 200 wins and cancels the other!
        """
        if not enable_hedge or hedge_delay <= 0 or hedge_delay >= timeout:
            return await self.fetch_page(url, timeout=timeout)

        # 1. Start primary probe
        p1_task = asyncio.create_task(self.fetch_page(url, timeout=timeout))

        # Wait initial hedge_delay
        try:
            done, _ = await asyncio.wait([p1_task], timeout=hedge_delay)
            if p1_task in done:
                res = p1_task.result()
                if res and res[1] == 200:
                    return res
        except Exception:
            pass

        # If primary probe completed early
        if p1_task.done():
            try:
                res = p1_task.result()
                if res and res[1] == 200:
                    return res
            except Exception:
                pass
            p1_task = asyncio.create_task(self.fetch_page(url, timeout=max(1.0, timeout - hedge_delay)))

        # 2. Race primary probe against independent looping hedge probes
        winner_result: Optional[Tuple[Optional[str], int]] = None
        winner_event = asyncio.Event()

        async def _watch_p1():
            nonlocal winner_result
            try:
                res = await p1_task
                if res and res[1] == 200 and not winner_event.is_set():
                    winner_result = res
                    winner_event.set()
            except (asyncio.CancelledError, Exception):
                pass

        async def _run_hedge_probes():
            nonlocal winner_result
            h_timeout = max(0.5, hedge_timeout)
            while not winner_event.is_set():
                h_task = asyncio.create_task(self.fetch_page(url, timeout=h_timeout))
                try:
                    done, _ = await asyncio.wait([h_task], timeout=h_timeout)
                    if h_task in done:
                        res = h_task.result()
                        if res and res[1] == 200 and not winner_event.is_set():
                            winner_result = res
                            winner_event.set()
                            return
                    else:
                        # Hedge probe timed out! Cancel stalled probe and retry next hedge probe
                        h_task.cancel()
                        try:
                            await h_task
                        except (asyncio.CancelledError, Exception):
                            pass
                except (asyncio.CancelledError, Exception):
                    h_task.cancel()
                    break

                if not winner_event.is_set():
                    await asyncio.sleep(0.05)

        p1_watcher = asyncio.create_task(_watch_p1())
        hedge_runner = asyncio.create_task(_run_hedge_probes())

        total_rem = max(0.5, timeout - hedge_delay)
        try:
            await asyncio.wait_for(winner_event.wait(), timeout=total_rem)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            p1_watcher.cancel()
            hedge_runner.cancel()
            if not p1_task.done():
                p1_task.cancel()

        if winner_result and winner_result[1] == 200:
            return winner_result

        if p1_task.done():
            try:
                res = p1_task.result()
                if res and res[1] == 200:
                    return res
            except Exception:
                pass

        return None, 0

    def check_maintenance(self, html: Optional[str], status_code: int) -> bool:
        """
        Precise detection of FC2 Cart maintenance / storefront closure:
        - Differentiates between transient 502/503 server congestion vs genuine store closure!
        - Transient 502/503/504 errors mean the server is busy, NOT under intentional maintenance.
        - True store maintenance replaces the storefront with an explicit maintenance page.
        """
        # Transient network/gateway errors: server busy, not store maintenance!
        if status_code in (0, 408, 429, 500, 502, 503, 504, 520, 521, 522, 524):
            return False

        if not html or len(html.strip()) < 50:
            return False

        html_lower = html.lower()

        # Check explicit store-closure keywords in HTML
        for kw in MAINTENANCE_KEYWORDS:
            if kw.lower() in html_lower:
                return True

        # Check dedicated maintenance DOM container
        if "#maintenance" in html_lower or "class=\"maintenance" in html_lower or "class='maintenance" in html_lower:
            return True

        return False

    def has_items_container(self, html: Optional[str]) -> bool:
        """Checks if standard FC2 catalog product list containers exist in the rendered HTML"""
        if not html:
            return False
        html_lower = html.lower()
        return (
            "#itemlist_page" in html_lower or
            "class=\"item_list\"" in html_lower or
            "class='item_list'" in html_lower or
            "class=\"item\"" in html_lower or
            "class='item'" in html_lower or
            "dt class=\"name\"" in html_lower or
            "dt class='name'" in html_lower or
            "dl class=\"item\"" in html_lower or
            "dl class='item'" in html_lower
        )

    def has_detail_container(self, html: Optional[str]) -> bool:
        """Checks if standard FC2 product detail containers exist in the rendered HTML"""
        if not html:
            return False
        html_lower = html.lower()
        return (
            "id=\"itemdetail_page\"" in html_lower or
            "id='itemdetail_page'" in html_lower or
            "class=\"item_detail\"" in html_lower or
            "class='item_detail'" in html_lower or
            "name=\"item_id\"" in html_lower or
            "name='item_id'" in html_lower or
            "class=\"btn_submit_cart\"" in html_lower or
            "class='btn_submit_cart'" in html_lower
        )

    def parse_items_from_html(self, html: str) -> List[ShopItem]:
        soup = BeautifulSoup(html, DEFAULT_PARSER)
        items: List[ShopItem] = []

        item_nodes = soup.select(".item_list li.item, .item_list .item")
        if not item_nodes:
            item_nodes = soup.select("#itemlist_page dl, .contents .item")

        for node in item_nodes:
            # 1. Title and link
            name_node = node.select_one("dt.name a, .name a, a[href*='/ca']")
            if not name_node:
                continue
            title = name_node.get_text(strip=True)
            relative_url = name_node.get("href", "")
            full_url = urljoin(self.base_url, relative_url)

            # 2. Extract item_id
            item_id = ""
            category_id = None
            match = re.search(r"/ca(\d+)/(\d+)/", relative_url)
            if match:
                category_id = match.group(1)
                item_id = match.group(2)
            else:
                hidden_id = node.select_one("input[name='item_id']")
                if hidden_id and hidden_id.get("value"):
                    item_id = hidden_id["value"]
                else:
                    id_match = re.search(r"/(\d+)/", relative_url)
                    if id_match:
                        item_id = id_match.group(1)

            if not item_id:
                continue

            # 3. Price
            price_node = node.select_one(".price, .price_replace")
            price = price_node.get_text(strip=True) if price_node else "N/A"

            # 4. Image
            img_node = node.select_one(".item_image img, img")
            img_url = ""
            if img_node:
                img_url = img_node.get("src") or img_node.get("data-src") or ""
                if img_url and not img_url.startswith("http"):
                    img_url = urljoin(self.base_url, img_url)

            # 5. Stock status
            stock_node = node.select_one("dd.stock, .stock")
            stock_text = stock_node.get_text(strip=True) if stock_node else ""

            has_cart_button = bool(node.select_one(".btn_submit_cart, input[value='カートに入れる'], a[href*='cart_in']"))

            # Determine upcoming vs sold out vs in stock
            is_upcoming = any(kw in stock_text for kw in UPCOMING_KEYWORDS)
            has_soldout = any(kw in stock_text for kw in SOLDOUT_KEYWORDS)
            has_instock_kw = any(kw in stock_text for kw in INSTOCK_KEYWORDS)

            scheduled_time = None
            if is_upcoming:
                # Scheduled / Upcoming: strictly NOT in stock
                in_stock = False
                m = re.search(r"([0-9０-９一二三四五六七八九十月日時分:\s~～ー]+?)(?:開始予定|発売予定|販売予定|予定)", stock_text)
                if m and m.group(1).strip():
                    scheduled_time = m.group(1).strip()
                else:
                    scheduled_time = stock_text
            elif has_soldout:
                in_stock = False
            elif has_cart_button or has_instock_kw:
                in_stock = True
            else:
                in_stock = has_cart_button

            item = ShopItem(
                item_id=str(item_id),
                title=title,
                price=price,
                url=full_url,
                image_url=img_url,
                in_stock=in_stock,
                stock_text=stock_text if stock_text else ("在庫あり" if in_stock else "在庫切れ"),
                category_id=category_id,
                is_upcoming=is_upcoming,
                scheduled_time=scheduled_time
            )
            items.append(item)

        return items

    def parse_single_item_page(self, html: str, url: str) -> Optional[ShopItem]:
        """Parses a single product detail page (/ca{cat}/{id}/) directly for zero-latency stock checks"""
        if not self.has_detail_container(html):
            return None

        soup = BeautifulSoup(html, DEFAULT_PARSER)
        item_id = ""
        category_id = None
        match = re.search(r"/ca(\d+)/(\d+)/", url)
        if match:
            category_id = match.group(1)
            item_id = match.group(2)
        else:
            id_match = re.search(r"/(\d+)/", url)
            if id_match:
                item_id = id_match.group(1)

        hidden_id = soup.select_one("input[name='item_id']")
        if hidden_id and hidden_id.get("value"):
            item_id = hidden_id["value"]

        if not item_id:
            return None

        title_node = soup.select_one("#itemdetail_page h2, #item_detail_page h2, .item_detail h2, .contents h2, h2.name, dt.name, title")
        title = title_node.get_text(strip=True) if title_node else f"商品-{item_id}"
        if " - " in title:
            title = title.split(" - ")[0].strip()

        price_node = soup.select_one(".price, .price_replace, dd.price")
        price = price_node.get_text(strip=True) if price_node else "N/A"

        img_node = soup.select_one(".item_images img, .thumb img, img")
        img_url = ""
        if img_node:
            img_url = img_node.get("src") or img_node.get("data-src") or ""
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(self.base_url, img_url)

        stock_node = soup.select_one("dd.stock, .stock")
        stock_text = stock_node.get_text(strip=True) if stock_node else ""

        has_cart_button = bool(soup.select_one(".btn_submit_cart, input[value='カートに入れる']"))

        is_upcoming = any(kw in stock_text for kw in UPCOMING_KEYWORDS)
        has_soldout = any(kw in stock_text for kw in SOLDOUT_KEYWORDS)
        has_instock_kw = any(kw in stock_text for kw in INSTOCK_KEYWORDS)

        scheduled_time = None
        if is_upcoming:
            in_stock = False
            m = re.search(r"([0-9０-９一二三四五六七八九十月日時分:\s~～ー]+?)(?:開始予定|発売予定|販売予定|予定)", stock_text)
            if m and m.group(1).strip():
                scheduled_time = m.group(1).strip()
            else:
                scheduled_time = stock_text
        elif has_soldout:
            in_stock = False
        elif has_cart_button or has_instock_kw:
            in_stock = True
        else:
            in_stock = has_cart_button

        return ShopItem(
            item_id=str(item_id),
            title=title,
            price=price,
            url=url,
            image_url=img_url,
            in_stock=in_stock,
            stock_text=stock_text if stock_text else ("在庫あり" if in_stock else "在庫切れ"),
            category_id=category_id,
            is_upcoming=is_upcoming,
            scheduled_time=scheduled_time
        )

    async def check_item_detail(
        self,
        item_url: str,
        timeout: Optional[float] = None,
        hedge_delay: Optional[float] = None,
        hedge_timeout: Optional[float] = None,
        enable_hedge: bool = False
    ) -> Tuple[Optional[ShopItem], bool]:
        """
        Fetches and checks a single product detail page directly, with optional hedged requests.
        Returns: (ShopItem or None, is_maintenance: bool)
        """
        if enable_hedge and hedge_delay is not None:
            html, status = await self.fetch_page_hedged(
                item_url,
                timeout=timeout or 4.0,
                hedge_delay=hedge_delay,
                hedge_timeout=hedge_timeout or 2.0,
                enable_hedge=True
            )
        else:
            html, status = await self.fetch_page(item_url, timeout=timeout)

        if not html:
            return None, False

        # 1. Detect store maintenance / closure page via standard keywords and DOM
        is_maint = self.check_maintenance(html, status)

        # 2. Redundancy fallback: If standard keywords were missed but the product detail container
        # (#itemdetail_page, .item_detail, input[name='item_id']) is completely absent,
        # check if generic closure / maintenance cues exist on the page
        if not is_maint and status == 200 and len(html.strip()) > 30:
            if not self.has_detail_container(html):
                closure_cues = ["準備", "閉店", "メンテナンス", "休業", "休止", "改装", "閉鎖"]
                if any(cue in html for cue in closure_cues):
                    is_maint = True

        if is_maint:
            return None, True

        if status != 200:
            return None, False

        item = self.parse_single_item_page(html, item_url)
        return item, False

    async def scan_full_catalog(self, max_pages: int = 25) -> List[ShopItem]:
        """
        Scans all pages in the store in parallel, ordered by sort=5 (newest update first).
        Dynamically detects total page count from page 0 to automatically adapt as items increase.
        """
        # 1. Fetch page 0 first to discover maximum page count from pagination
        page0_html, status = await self.fetch_page(f"{self.base_url}/?sort=5&page=0")
        if not page0_html:
            return []

        all_items: List[ShopItem] = []
        seen_ids = set()
        for it in self.parse_items_from_html(page0_html):
            if it.item_id not in seen_ids:
                seen_ids.add(it.item_id)
                all_items.append(it)
                if it.item_id not in self.known_items:
                    self.known_items[it.item_id] = it

        # Detect total items count from .pagerbox p (e.g. <p>（全商品239件）</p>) or category count
        soup0 = BeautifulSoup(page0_html, DEFAULT_PARSER)
        pager_p = soup0.select_one(".pagerbox p")
        total_items_count = None
        if pager_p:
            m = re.search(r"(\d+)", pager_p.get_text())
            if m:
                total_items_count = int(m.group(1))

        if not total_items_count:
            # Fallback regex for "全商品(\d+)件" or "全表示 \((\d+)\)"
            m = re.search(r"全[^\(\d]{0,10}\(?(\d+)\)?", page0_html)
            if m:
                total_items_count = int(m.group(1))

        if total_items_count and total_items_count > 0:
            # FC2 lists 20 items per page
            total_pages = min(max_pages, math.ceil(total_items_count / 20))
        else:
            page_nums = [int(p) for p in re.findall(r"page=(\d+)", page0_html)]
            total_pages = min(max_pages, max(page_nums) + 1 if page_nums else 12)

        # 2. Fetch remaining pages (page 1 to total_pages - 1) in parallel
        if total_pages > 1:
            sem = asyncio.Semaphore(5)

            async def _fetch_with_sem(p: int):
                async with sem:
                    return await self.fetch_page(f"{self.base_url}/?sort=5&page={p}")

            tasks = [_fetch_with_sem(p) for p in range(1, total_pages)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, tuple) and res[0]:
                    html, _ = res
                    for it in self.parse_items_from_html(html):
                        if it.item_id not in seen_ids:
                            seen_ids.add(it.item_id)
                            all_items.append(it)
                            if it.item_id not in self.known_items:
                                self.known_items[it.item_id] = it

        return all_items

    def matches_rules(
        self,
        item: ShopItem,
        target_keywords: List[str],
        exclude_keywords: List[str],
        min_price: Optional[int] = None,
        max_price: Optional[int] = None
    ) -> bool:
        """Checks if a ShopItem matches target/exclude keywords and price range"""
        title_lower = item.title.lower()

        # 1. Exclude keywords check
        if any(exc.lower() in title_lower for exc in exclude_keywords if exc):
            return False

        # 2. Target keywords check
        if target_keywords:
            def _kw_check(kw: str) -> bool:
                kw_clean = kw.strip().lower()
                if not kw_clean:
                    return False
                if kw_clean in title_lower:
                    return True
                # Smart doll name matching: allow matching 'ひな' when keyword is 'ひなちゃん'
                if kw_clean.endswith("ちゃん") and len(kw_clean) > 3:
                    base_name = kw_clean[:-3].strip()
                    if base_name in title_lower:
                        return True
                return False

            if not any(_kw_check(kw) for kw in target_keywords):
                return False

        # 3. Price range check (0 or None means unlimited)
        pv = item.price_value
        if min_price and min_price > 0:
            if pv is None or pv < min_price:
                return False
        if max_price and max_price > 0:
            if pv is None or pv > max_price:
                return False

        return True

    def check_matches(
        self,
        current_items: List[ShopItem],
        target_keywords: List[str],
        exclude_keywords: List[str],
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        force_instock_trigger: bool = False
    ) -> List[Tuple[str, ShopItem]]:
        """
        Compares current items with known snapshot.
        Detects:
        - Upcoming item officially dropping ("UPCOMING_DROPPED") -> Top Priority Auto-Order!
        - Brand new in-stock items ("NEW_IN_STOCK")
        - Restocked existing items ("RESTOCKED")
        - Newly scheduled upcoming items ("NEW_UPCOMING")
        - New out-of-stock announcements ("NEW_OUT_OF_STOCK")
        Filters items by keywords and price range (min_price and max_price in JPY).
        """
        triggers: List[Tuple[str, ShopItem]] = []

        for item in current_items:
            if not self.matches_rules(item, target_keywords, exclude_keywords, min_price, max_price):
                continue

            # If force trigger is active (e.g. right after maintenance ends)
            if force_instock_trigger and item.in_stock:
                triggers.append(("NEW_IN_STOCK", item))
                self.known_items[item.item_id] = item
                continue

            # Regular comparison against snapshot
            if item.item_id not in self.known_items:
                if self.is_initialized:
                    if item.in_stock:
                        triggers.append(("NEW_IN_STOCK", item))
                    elif item.is_upcoming:
                        triggers.append(("NEW_UPCOMING", item))
                    else:
                        triggers.append(("NEW_OUT_OF_STOCK", item))
                else:
                    # On first startup: if item is already in stock, trigger it!
                    if item.in_stock:
                        triggers.append(("NEW_IN_STOCK", item))
                    elif item.is_upcoming:
                        triggers.append(("NEW_UPCOMING", item))
                self.known_items[item.item_id] = item
            else:
                prev_item = self.known_items[item.item_id]
                # Transition 1: Previously UPCOMING (or scheduled) and now IN STOCK!
                if prev_item.is_upcoming and item.in_stock:
                    triggers.append(("UPCOMING_DROPPED", item))
                # Transition 2: Previously sold out and now restocked!
                elif not prev_item.in_stock and item.in_stock:
                    triggers.append(("RESTOCKED", item))
                # Transition 3: Newly scheduled as upcoming from sold out
                elif not prev_item.is_upcoming and item.is_upcoming:
                    triggers.append(("NEW_UPCOMING", item))
                # Update snapshot
                self.known_items[item.item_id] = item

        if not self.is_initialized:
            self.is_initialized = True

        return triggers

    async def check_once(
        self,
        monitor_url: str,
        target_keywords: List[str],
        exclude_keywords: List[str],
        scan_pages: int = 2,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        force_instock_trigger: bool = False,
        enable_empty_catalog_check: bool = True,
        empty_catalog_threshold: int = 2
    ) -> Tuple[List[ShopItem], List[Tuple[str, ShopItem]], bool, str]:
        """
        Performs one monitoring scan.
        Supports multi-page scanning for top pages where drops occur.
        Returns: (parsed_items, trigger_events, is_maintenance, maintenance_reason)
        """
        # If scan_pages == 1: single request
        if scan_pages <= 1:
            html, status = await self.fetch_page(monitor_url)
            if self.check_maintenance(html, status):
                self.empty_catalog_consecutive = 0
                return [], [], True, "官方维护公告标识"
            if not html:
                return [], [], False, f"网络错误 (HTTP {status})"
            items = self.parse_items_from_html(html)

            # Check consecutive empty catalog anomaly (shop emptied/hidden for restocking)
            if enable_empty_catalog_check and status == 200 and len(html.strip()) > 300:
                if len(items) == 0 and not self.has_items_container(html):
                    self.empty_catalog_consecutive += 1
                    if self.empty_catalog_consecutive >= empty_catalog_threshold:
                        return [], [], True, f"商品全量清空 (连续{self.empty_catalog_consecutive}次空页面确认)"
                    else:
                        return [], [], False, f"空页面警告 (第{self.empty_catalog_consecutive}/{empty_catalog_threshold}次)"
                else:
                    self.empty_catalog_consecutive = 0
            elif len(items) > 0:
                self.empty_catalog_consecutive = 0
        else:
            # Parallel scan of top pages (e.g. page 0 and page 1)
            base_query = monitor_url.split("?")[0]
            tasks = [self.fetch_page(f"{base_query}?sort=5&page={p}") for p in range(scan_pages)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            items = []
            seen_ids = set()
            first_page_html = ""
            first_page_status = 0
            for idx, res in enumerate(results):
                if isinstance(res, tuple):
                    html, status = res
                    if idx == 0:
                        first_page_html = html
                        first_page_status = status
                    if self.check_maintenance(html, status):
                        self.empty_catalog_consecutive = 0
                        return [], [], True, "官方维护公告标识"
                    if html:
                        for it in self.parse_items_from_html(html):
                            if it.item_id not in seen_ids:
                                seen_ids.add(it.item_id)
                                items.append(it)

            # Check consecutive empty catalog anomaly on page 0
            if enable_empty_catalog_check and first_page_status == 200 and first_page_html and len(first_page_html.strip()) > 300:
                if len(items) == 0 and not self.has_items_container(first_page_html):
                    self.empty_catalog_consecutive += 1
                    if self.empty_catalog_consecutive >= empty_catalog_threshold:
                        return [], [], True, f"商品全量清空 (连续{self.empty_catalog_consecutive}次空页面确认)"
                    else:
                        return [], [], False, f"空页面警告 (第{self.empty_catalog_consecutive}/{empty_catalog_threshold}次)"
                else:
                    self.empty_catalog_consecutive = 0
            elif len(items) > 0:
                self.empty_catalog_consecutive = 0

        triggers = self.check_matches(
            items,
            target_keywords=target_keywords,
            exclude_keywords=exclude_keywords,
            min_price=min_price,
            max_price=max_price,
            force_instock_trigger=force_instock_trigger
        )
        return items, triggers, False, ""
