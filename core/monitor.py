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

class FC2Monitor:
    def __init__(self, base_url: str = "https://mamachapptoy.cart.fc2.com", client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        self.client = client
        self.known_items: Dict[str, ShopItem] = {}
        self.is_initialized = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(10.0, connect=5.0),
                http2=HAS_HTTP2,
                follow_redirects=True
            )
        return self.client

    async def fetch_page(self, url: str) -> Optional[str]:
        client = await self._get_client()
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
            else:
                print(f"[Monitor] Fetch {url} returned status {resp.status_code}")
                return None
        except Exception as e:
            print(f"[Monitor] Network error fetching {url}: {e}")
            return None

    def parse_items_from_html(self, html: str) -> List[ShopItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: List[ShopItem] = []

        # FC2 standard layout: .item_list li.item
        item_nodes = soup.select(".item_list li.item, .item_list .item")
        if not item_nodes:
            # Fallback selector for alternative template styles
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
            # Pattern: /ca<cat>/<item_id>/...
            item_id = ""
            category_id = None
            match = re.search(r"/ca(\d+)/(\d+)/", relative_url)
            if match:
                category_id = match.group(1)
                item_id = match.group(2)
            else:
                # Try hidden inputs if present
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
            # If stock_text does not contain "在庫切れ" and there is stock info or cart button, it's in stock.
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

    def check_matches(
        self,
        current_items: List[ShopItem],
        target_keywords: List[str],
        exclude_keywords: List[str]
    ) -> List[Tuple[str, ShopItem]]:
        """
        Compares current items with known snapshot.
        Returns a list of tuples: (event_type, item)
        event_type can be:
        - "NEW_IN_STOCK": Newly listed item that has stock
        - "RESTOCKED": Previously known item whose stock transitioned from False -> True
        - "NEW_OUT_OF_STOCK": Newly listed item, currently out of stock (for notifications)
        """
        triggers: List[Tuple[str, ShopItem]] = []

        for item in current_items:
            # Check keyword filters
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

            # Compare against known snapshot
            if item.item_id not in self.known_items:
                if self.is_initialized:
                    if item.in_stock:
                        triggers.append(("NEW_IN_STOCK", item))
                    else:
                        triggers.append(("NEW_OUT_OF_STOCK", item))
                # Add to known
                self.known_items[item.item_id] = item
            else:
                prev_item = self.known_items[item.item_id]
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
        exclude_keywords: List[str]
    ) -> Tuple[List[ShopItem], List[Tuple[str, ShopItem]]]:
        """
        Performs one monitoring scan.
        Returns: (all_parsed_items, trigger_events)
        """
        html = await self.fetch_page(monitor_url)
        if not html:
            return [], []

        items = self.parse_items_from_html(html)
        triggers = self.check_matches(items, target_keywords, exclude_keywords)
        return items, triggers

    async def verify_item_stock_direct(self, item_url: str) -> bool:
        """
        Fetches the item detail page to verify real-time stock status.
        Useful for high-confidence stock checks before triggering checkout.
        """
        html = await self.fetch_page(item_url)
        if not html:
            return False

        soup = BeautifulSoup(html, "html.parser")
        stock_node = soup.select_one("li.stock, .stock")
        if stock_node and ("在庫切れ" in stock_node.text or "完売" in stock_node.text):
            return False

        # Check form data-submit
        basket_form = soup.select_one("form.basket")
        if basket_form:
            submit_data = basket_form.get("data-submit")
            if submit_data == "0":
                return False

        return True
