import re
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
    "メンテナンス",
    "只今メンテナンス中",
    "ただいまメンテナンス",
    "メンテナンスを行っております",
    "只今、メンテナンス中です",
    "ショップ準備中",
    "開店準備中",
    "準備中",
    "改装中",
    "休止中",
    "一時休止",
    "一時閉店",
    "閉店中",
    "休業中",
    "販売準備",
    "商品入れ替え",
    "しばらくお待ち",
    "カートを閉",
    "閉鎖中",
    "再開まで",
    "closed",
    "close",
    "maintenance",
    "under maintenance",
    "coming soon"
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

    async def fetch_page(self, url: str) -> Tuple[Optional[str], int]:
        client = await self._get_client()
        for attempt in range(2):
            try:
                resp = await client.get(url)
                return resp.text, resp.status_code
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                err_msg = str(e) if str(e) else type(e).__name__
                print(f"[Monitor] Network error fetching {url}: {err_msg}")
                return None, 0
        return None, 0

    def check_maintenance(self, html: Optional[str], status_code: int) -> bool:
        """
        Multi-layered bulletproof detection of FC2 Cart maintenance / storefront closure:
        1. HTTP status code check (502, 503, 504, 423)
        2. Broad keyword bank matching across full HTML text and title
        3. Structural anomaly detection: on a store catalog page (?sort=5), if the normal
           product container (.item_list, #itemlist_page, .item, etc.) is completely absent,
           the storefront is closed for maintenance/restocking regardless of custom wording.
        """
        if status_code in (502, 503, 504, 423):
            return True
        if not html or len(html.strip()) < 50:
            return False

        html_lower = html.lower()

        # 1. Check explicit maintenance & closure keywords
        for kw in MAINTENANCE_KEYWORDS:
            if kw.lower() in html_lower:
                return True

        # 2. Structural check: does the catalog page contain standard FC2 product containers?
        has_items_container = (
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

        # On Mamachapp catalog (which normally has 230+ products), if the entire product container
        # is missing from the rendered HTML, the store is 100% in pre-drop maintenance / closed
        if not has_items_container:
            return True

        return False

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

    async def check_item_detail(self, item_url: str) -> Optional[ShopItem]:
        """Fetches and checks a single product detail page directly"""
        html, status = await self.fetch_page(item_url)
        if not html or status != 200:
            return None
        return self.parse_single_item_page(html, item_url)

    async def scan_full_catalog(self, max_pages: int = 15) -> List[ShopItem]:
        """
        Scans all pages in the store in parallel, ordered by sort=5 (newest update first).
        Covers the entire product inventory (230+ items).
        """
        sem = asyncio.Semaphore(3)

        async def _fetch_with_sem(p: int):
            async with sem:
                return await self.fetch_page(f"{self.base_url}/?sort=5&page={p}")

        tasks = [_fetch_with_sem(p) for p in range(max_pages)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: List[ShopItem] = []
        seen_ids = set()

        for res in results:
            if isinstance(res, tuple) and res[0]:
                html, _ = res
                page_items = self.parse_items_from_html(html)
                for it in page_items:
                    if it.item_id not in seen_ids:
                        seen_ids.add(it.item_id)
                        all_items.append(it)
                        # Also register in known_items so monitor knows them
                        if it.item_id not in self.known_items:
                            self.known_items[it.item_id] = it

        return all_items

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
            title_lower = item.title.lower()

            # 1. Exclude keywords check
            if any(exc.lower() in title_lower for exc in exclude_keywords if exc):
                continue

            # 2. Target keywords check
            matches_target = True
            if target_keywords:
                matches_target = any(kw.lower() in title_lower for kw in target_keywords if kw)

            if not matches_target:
                continue

            # 3. Price range check (0 or None means unlimited)
            pv = item.price_value
            if min_price and min_price > 0:
                if pv is None or pv < min_price:
                    continue
            if max_price and max_price > 0:
                if pv is None or pv > max_price:
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
        force_instock_trigger: bool = False
    ) -> Tuple[List[ShopItem], List[Tuple[str, ShopItem]], bool]:
        """
        Performs one monitoring scan.
        Supports multi-page scanning for top pages where drops occur.
        Returns: (parsed_items, trigger_events, is_maintenance)
        """
        # If scan_pages == 1: single request
        if scan_pages <= 1:
            html, status = await self.fetch_page(monitor_url)
            if self.check_maintenance(html, status):
                return [], [], True
            if not html:
                return [], [], False
            items = self.parse_items_from_html(html)
        else:
            # Parallel scan of top pages (e.g. page 0 and page 1)
            base_query = monitor_url.split("?")[0]
            tasks = [self.fetch_page(f"{base_query}?sort=5&page={p}") for p in range(scan_pages)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            items = []
            seen_ids = set()
            for res in results:
                if isinstance(res, tuple):
                    html, status = res
                    if self.check_maintenance(html, status):
                        return [], [], True
                    if html:
                        for it in self.parse_items_from_html(html):
                            if it.item_id not in seen_ids:
                                seen_ids.add(it.item_id)
                                items.append(it)

        triggers = self.check_matches(
            items,
            target_keywords=target_keywords,
            exclude_keywords=exclude_keywords,
            min_price=min_price,
            max_price=max_price,
            force_instock_trigger=force_instock_trigger
        )
        return items, triggers, False
