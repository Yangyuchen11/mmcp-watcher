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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

MAINTENANCE_KEYWORDS = [
    "メンテナンス",
    "只今メンテナンス中",
    "ただいまメンテナンス",
    "メンテナンスを行っております",
    "只今、メンテナンス中です",
    "ショップ準備中"
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
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=limits,
                http2=HAS_HTTP2,
                follow_redirects=True
            )
        return self.client

    async def fetch_page(self, url: str) -> Tuple[Optional[str], int]:
        client = await self._get_client()
        try:
            resp = await client.get(url)
            return resp.text, resp.status_code
        except Exception as e:
            print(f"[Monitor] Network error fetching {url}: {e}")
            return None, 0

    def check_maintenance(self, html: Optional[str], status_code: int) -> bool:
        """Detects whether FC2 Cart is currently in store maintenance mode before a drop"""
        if status_code in (502, 503, 504):
            return True
        if not html:
            return False

        # If normal store product page container is missing and maintenance notice is present
        has_items_container = "#itemlist_page" in html or "class=\"item_list\"" in html or "class='item_list'" in html
        if not has_items_container:
            for kw in MAINTENANCE_KEYWORDS:
                if kw in html:
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

            # In FC2, "在庫切れ" means out of stock.
            has_soldout = "在庫切れ" in stock_text or "完売" in stock_text
            has_cart_button = bool(node.select_one(".btn_submit_cart, input[value='カートに入れる']"))

            in_stock = (not has_soldout) and (bool(stock_text) or has_cart_button)

            item = ShopItem(
                item_id=str(item_id),
                title=title,
                price=price,
                url=full_url,
                image_url=img_url,
                in_stock=in_stock,
                stock_text=stock_text if stock_text else ("在庫切れ" if not in_stock else "在庫あり"),
                category_id=category_id
            )
            items.append(item)

        return items

    async def scan_full_catalog(self, max_pages: int = 15) -> List[ShopItem]:
        """
        Scans all pages in the store in parallel, ordered by sort=5 (newest update first).
        Covers the entire product inventory (230+ items).
        """
        tasks = [self.fetch_page(f"{self.base_url}/?sort=5&page={p}") for p in range(max_pages)]
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
        force_instock_trigger: bool = False
    ) -> List[Tuple[str, ShopItem]]:
        """
        Compares current items with known snapshot.
        Detects:
        - Brand new in-stock items ("NEW_IN_STOCK")
        - Restocked existing items ("RESTOCKED")
        - New out-of-stock announcements ("NEW_OUT_OF_STOCK")
        """
        triggers: List[Tuple[str, ShopItem]] = []

        for item in current_items:
            title_lower = item.title.lower()

            # Exclude check
            if any(exc.lower() in title_lower for exc in exclude_keywords if exc):
                continue

            # Target keywords check
            matches_target = True
            if target_keywords:
                matches_target = any(kw.lower() in title_lower for kw in target_keywords if kw)

            if not matches_target:
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
                    else:
                        triggers.append(("NEW_OUT_OF_STOCK", item))
                else:
                    # On first startup: if item is already in stock, trigger it!
                    if item.in_stock:
                        triggers.append(("NEW_IN_STOCK", item))
                self.known_items[item.item_id] = item
            else:
                prev_item = self.known_items[item.item_id]
                # If an existing item transitioned from out-of-stock to in-stock!
                if not prev_item.in_stock and item.in_stock:
                    triggers.append(("RESTOCKED", item))
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

        triggers = self.check_matches(items, target_keywords, exclude_keywords, force_instock_trigger)
        return items, triggers, False
