import re
import time
import asyncio
import unicodedata
from typing import Dict, Any, Optional, Tuple, Callable
from urllib.parse import urljoin
from bs4 import BeautifulSoup, Comment
import httpx

try:
    import h2
    HAS_HTTP2 = True
except ImportError:
    HAS_HTTP2 = False

class FC2Checkout:
    def __init__(
        self,
        base_url: str = "https://mamachapptoy.cart.fc2.com",
        buyer_info: Optional[Dict[str, Any]] = None,
        dry_run: bool = True,
        retry_count: int = 3,
        retry_delay: float = 0.3
    ):
        self.base_url = base_url.rstrip("/")
        self.buyer_info = buyer_info or {}
        self.dry_run = dry_run
        self.retry_count = max(1, retry_count)
        self.retry_delay = max(0.05, retry_delay)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        # Dedicated client to maintain cookies / session for an order attempt
        # Snappy timeouts (3s connect, 5s read) prevent thread from hanging when server is congested
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            http2=False,  # HTTP/1.1 is strictly more stable for FC2 Cart CGI regi forms under heavy load
            follow_redirects=True
        )
        self.is_warmed: bool = False
        self.last_warmed_at: float = 0.0
        # Stage tracking for concurrency safety & duplicate-order prevention
        self.steps_completed: int = 0
        self.in_final_stage: bool = False
        self.final_submitted: bool = False
        self.before_final_submit: Optional[Callable[[], Any]] = None
        self._cached_confirm_html: Optional[str] = None
        self._cached_confirm_soup: Optional[BeautifulSoup] = None

    def reset_for_next_attempt(self):
        """
        Resets FC2 session cookies and step state for a fresh checkout attempt while
        preserving the underlying TCP + TLS 1.3 Keep-Alive connection pool (0ms handshake!).
        """
        if self.client and not self.client.is_closed:
            self.client.cookies.clear()
        self.steps_completed = 0
        self.in_final_stage = False
        self.final_submitted = False
        self._cached_confirm_html = None
        self._cached_confirm_soup = None

    async def warm_up(self, target_url: Optional[str] = None) -> bool:
        """
        Pre-warms the TCP and TLS 1.3 connection to the cart server by performing a fast lightweight HEAD or GET.
        Keeps the SSL socket open and hot in the connection pool so the next POST /cart_in has 0ms handshake overhead!
        """
        try:
            url = target_url or f"{self.base_url}/"
            # Fast HEAD request to open and maintain the TLS keep-alive pipeline
            resp = await self.client.head(url, timeout=3.5)
            if resp.status_code in (200, 301, 302, 403, 405):
                self.is_warmed = True
                self.last_warmed_at = time.time()
                return True
            # Fallback to GET if server rejects HEAD
            await self.client.get(url, timeout=3.5)
            self.is_warmed = True
            self.last_warmed_at = time.time()
            return True
        except Exception:
            return False

    async def close(self):
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    @staticmethod
    def _select_checkout_form(soup: BeautifulSoup):
        """
        Selects the genuine FC2 checkout/confirmation POST form in strict priority order,
        never matching the header GET search form (<form name="form" method="get" action="/">).
        """
        return (
            soup.select_one("#confirm_page form[method='post'], #checkout_confirm_page form[method='post']")
            or soup.select_one("#checkout_page form[action*='regi']")
            or soup.select_one("#checkout_page form[method='post']")
            or soup.select_one("form[action*='regi']")
            or soup.select_one("form[method='post']:not([action='/']):not([action*='cart_in']):not([action*='check'])")
        )

    @staticmethod
    def _extract_radio_label(radio_tag, form_tag) -> str:
        """Extracts display text associated with an <input type='radio'> element."""
        parts = [radio_tag.get("value", "")]
        parent_label = radio_tag.find_parent("label")
        if parent_label:
            parts.append(parent_label.get_text(" ", strip=True))
        rid = radio_tag.get("id")
        if rid and form_tag:
            for_label = form_tag.find("label", attrs={"for": rid})
            if for_label:
                parts.append(for_label.get_text(" ", strip=True))
        nxt = radio_tag.next_sibling
        if nxt:
            nxt_txt = nxt.get_text(" ", strip=True) if hasattr(nxt, "get_text") else str(nxt).strip()
            if nxt_txt:
                parts.append(nxt_txt)
        return " ".join(p for p in parts if p)

    @classmethod
    def _pick_radio_value(cls, radios, preferred: str, form_tag) -> str:
        if not radios:
            return ""
        # Pass 1: Match user's preferred text against value or label text
        if preferred:
            for r in radios:
                label_txt = cls._extract_radio_label(r, form_tag)
                if preferred in label_txt:
                    return r.get("value", "")
        # Pass 2: Fallback to default checked radio
        for r in radios:
            if "checked" in r.attrs:
                return r.get("value", "")
        # Pass 3: First available radio
        return radios[0].get("value", "")

    @staticmethod
    def _crop_checkout_region(html: str, strip_prefecture_select: bool = False) -> str:
        """
        Crops HTML to the main checkout/confirmation container and strips #sidemenu/#footer
        so BeautifulSoup only parses the form region instead of the full 35KB storefront template.
        """
        idx = -1
        for marker in ('id="checkout_page"', "id='checkout_page'", 'id="confirm_page"', "id='confirm_page'", 'id="checkout_confirm_page"'):
            idx = html.find(marker)
            if idx != -1:
                break
        if idx != -1:
            start = html.rfind("<div", 0, idx)
            if start == -1:
                start = idx
            end = html.find('id="sidemenu"', idx)
            if end == -1:
                end = html.find('id="footer"', idx)
            region = html[start:end] if end != -1 else html[start:]
        else:
            region = html

        if strip_prefecture_select and "address[in][ken]" in region:
            region = re.sub(
                r'<select[^>]*name=["\']address\[in\]\[ken\]["\'][^>]*>.*?</select>',
                "",
                region,
                flags=re.DOTALL | re.IGNORECASE
            )
        return region

    @staticmethod
    def _is_cart_soldout(html: str) -> bool:
        """
        Checks if a cart response indicates genuine out-of-stock without false-flagging
        products whose title contains '完売' (e.g. '【完売御礼・再販】...').
        """
        if not html or not any(kw in html for kw in ("在庫切れ", "売り切れ", "完売", "品切れ", "售罄")):
            return False

        explicit_phrases = [
            "在庫切れのため", "在庫切れ（", "在庫切れとな", "カートに入れることができません",
            "売り切れました", "品切れのため", "現在、在庫切れ"
        ]
        if any(p in html for p in explicit_phrases):
            return True

        soup = BeautifulSoup(html, "html.parser")
        for err_el in soup.select(".errormessage, .error, font[color*='ff0000'], font[color='red'], font[color*='FF0000']"):
            err_txt = err_el.get_text(strip=True)
            if any(kw in err_txt for kw in ["在庫切れ", "売り切れ", "完売", "品切れ"]):
                return True

        has_checkout_entry = "/regi" in html or "btn_submit_checkout" in html or "レジへすすむ" in html
        if not has_checkout_entry and any(kw in html for kw in ["在庫切れ", "売り切れ", "完売", "品切れ"]):
            return True

        return False

    async def _verify_item_in_cart_after_timeout(self) -> bool:
        """
        When POST /cart_in times out, checks GET /check to see if the server already added the item
        to the session cart, preventing duplicate quantity accumulation on retry.
        """
        try:
            resp = await self.client.get(f"{self.base_url}/check", timeout=3.0)
            if resp.status_code == 200:
                html = resp.text
                if "カートに商品は入っていません" not in html and (
                    "/regi" in html or "btn_submit_checkout" in html or "レジへすすむ" in html
                ):
                    return True
        except Exception:
            pass
        return False

    async def add_to_cart(self, item_id: str, item_url: Optional[str] = None, quantity: int = 1) -> Tuple[bool, str]:
        """
        Step 1: Add item to FC2 Cart
        POST /cart_in with Submit_cart=true & item_id
        """
        cart_in_url = f"{self.base_url}/cart_in"
        post_data = {
            "Submit_cart": "true",
            "item_id": str(item_id),
            "order_su": str(quantity)
        }

        req_headers = {
            **self.headers,
            "Origin": self.base_url,
            "Referer": item_url if item_url else f"{self.base_url}/ca13/{item_id}/",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        last_error = ""
        prev_timed_out = False
        for attempt in range(1, self.retry_count + 1):
            try:
                if prev_timed_out:
                    if await self._verify_item_in_cart_after_timeout():
                        self.steps_completed = max(self.steps_completed, 1)
                        return True, f"[重试第{attempt}次成功] 加购成功(超时复核已在车内)！"
                    prev_timed_out = False

                t0 = time.perf_counter()
                resp = await self.client.post(cart_in_url, data=post_data, headers=req_headers)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                if resp.status_code != 200:
                    last_error = f"Cart_in HTTP Error {resp.status_code} ({elapsed_ms}ms)"
                    if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, last_error

                html = resp.text
                # Check for explicit out-of-stock messages (without false-matching product titles)
                if self._is_cart_soldout(html):
                    return False, f"加购失败：商品在庫切れ已售罄 ({elapsed_ms}ms)"

                # Check if cart is empty
                if "カートに商品は入っていません" in html:
                    last_error = f"加购未完成：购物车为空或尚未开放 ({elapsed_ms}ms)"
                    if attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, last_error

                # Check if valid checkout button / cart container exists
                if "/regi" in html or "btn_submit_checkout" in html or "レジへすすむ" in html or "checkcart_page" in html:
                    self.steps_completed = max(self.steps_completed, 1)
                    prefix = f"[重试第{attempt}次成功] " if attempt > 1 else ""
                    return True, f"{prefix}加购成功！耗时: {elapsed_ms}ms"

                # Otherwise it is a 200 OK busy page ("只今アクセスが集中") or maintenance page -> retry!
                last_error = f"加购未完成：收到繁忙或维护页面 ({elapsed_ms}ms)"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
                return False, last_error
            except Exception as e:
                if isinstance(e, httpx.TimeoutException):
                    prev_timed_out = True
                err_desc = str(e) if str(e) else type(e).__name__
                last_error = f"加购网络异常: {err_desc}"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
                if prev_timed_out and await self._verify_item_in_cart_after_timeout():
                    self.steps_completed = max(self.steps_completed, 1)
                    return True, "加购成功(超时后复核已在车内)！"
                return False, last_error

        return False, last_error

    async def get_regi_form(self) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """
        Step 2a: GET /regi to inspect the form, extract hidden fields, and identify
        payment method, delivery method, and delivery time options.
        Returns: (success, form_context, error_message)
        """
        regi_url = f"{self.base_url}/regi"
        last_error = ""

        for attempt in range(1, self.retry_count + 1):
            try:
                resp = await self.client.get(regi_url)
                if resp.status_code != 200:
                    last_error = f"GET /regi returned HTTP {resp.status_code}"
                    if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                html = resp.text
                if "カートにある商品が売り切れました" in html or "在庫切れのため" in html:
                    return False, None, "商品在结算时售罄 (カートにある商品が売り切れました)"

                # Detect session loss / redirect back to empty cart page
                if "カートに商品は入っていません" in html or (resp.url and resp.url.path == "/check"):
                    return False, None, "结算失败：购物车已空或会话丢失"

                region_html = self._crop_checkout_region(html, strip_prefecture_select=True)
                soup = BeautifulSoup(region_html, "html.parser")
                form = self._select_checkout_form(soup)

                if not form:
                    last_error = "GET /regi 未检测到有效结算表单 (页面繁忙或维护中)"
                    if attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                form_ctx: Dict[str, Any] = {
                    "payment_method": "",
                    "delivery_method": "",
                    "delivery_time": "",
                    "time_field_name": "",
                    "delivery_date": "",
                    "date_field_name": "",
                    "hidden_fields": {},
                    "extra_selects": {}
                }

                # 1. Capture all hidden inputs (tokens, session IDs, ticket tokens)
                for inp in form.find_all("input", type="hidden"):
                    name = inp.get("name")
                    val = inp.get("value", "")
                    if name and name != "Submit1":
                        form_ctx["hidden_fields"][name] = val

                preferred_pay = (
                    self.buyer_info.get("payment_method")
                    or self.buyer_info.get("preferred_payment")
                    or "代金引換"
                )
                preferred_deli = self.buyer_info.get("delivery_method", "宅急便")

                # Layer 1: Semantic Table Row Inspection (based on label text like '配送方法', 'お支払い方法')
                for tr in form.find_all("tr"):
                    sel = tr.find("select")
                    radios = tr.find_all("input", type="radio")
                    if not sel and not radios:
                        continue
                    header_cell = tr.find(["td", "th"])
                    label_text = header_cell.get_text(strip=True) if header_cell else ""

                    if "支払" in label_text:
                        if sel and not form_ctx["payment_method"]:
                            for opt in sel.find_all("option"):
                                v, t = opt.get("value", ""), opt.get_text(strip=True)
                                if preferred_pay and (preferred_pay in t or preferred_pay in v):
                                    form_ctx["payment_method"] = v
                                    break
                            if not form_ctx["payment_method"] and sel.find_all("option"):
                                for opt in sel.find_all("option"):
                                    if opt.get("value"):
                                        form_ctx["payment_method"] = opt.get("value")
                                        break
                        elif radios and not form_ctx["payment_method"]:
                            form_ctx["payment_method"] = self._pick_radio_value(radios, preferred_pay, form)

                    elif "配送" in label_text or "配達方法" in label_text:
                        if sel and not form_ctx["delivery_method"]:
                            for opt in sel.find_all("option"):
                                v, t = opt.get("value", ""), opt.get_text(strip=True)
                                if preferred_deli and (preferred_deli in t or preferred_deli in v):
                                    form_ctx["delivery_method"] = v
                                    break
                            if not form_ctx["delivery_method"] and sel.find_all("option"):
                                for opt in sel.find_all("option"):
                                    if opt.get("value"):
                                        form_ctx["delivery_method"] = opt.get("value")
                                        break
                        elif radios and not form_ctx["delivery_method"]:
                            form_ctx["delivery_method"] = self._pick_radio_value(radios, preferred_deli, form)

                    elif "時間" in label_text:
                        if sel and not form_ctx["delivery_time"]:
                            form_ctx["time_field_name"] = sel.get("name", "derivery[time]")
                            for opt in sel.find_all("option"):
                                t = opt.get_text(strip=True)
                                if "指定なし" in t or "希望なし" in t:
                                    form_ctx["delivery_time"] = opt.get("value", "")
                                    break
                            else:
                                first_opt = sel.find("option")
                                if first_opt:
                                    form_ctx["delivery_time"] = first_opt.get("value", "")

                    elif "日" in label_text and "配達" in label_text:
                        if sel and not form_ctx["delivery_date"]:
                            form_ctx["date_field_name"] = sel.get("name", "derivery[date]")
                            for opt in sel.find_all("option"):
                                t = opt.get_text(strip=True)
                                if "指定なし" in t or "希望なし" in t:
                                    form_ctx["delivery_date"] = opt.get("value", "")
                                    break
                            else:
                                first_opt = sel.find("option")
                                if first_opt:
                                    form_ctx["delivery_date"] = first_opt.get("value", "")

                # Layer 2: Fallback CSS Attribute Selectors
                if not form_ctx["payment_method"]:
                    pay_select = form.select_one("select[name='payment_method'], select[name='payment'], select[name*='pay']")
                    if pay_select:
                        for opt in pay_select.find_all("option"):
                            v, t = opt.get("value", ""), opt.get_text(strip=True)
                            if preferred_pay and (preferred_pay in t or preferred_pay in v):
                                form_ctx["payment_method"] = v
                                break
                        if not form_ctx["payment_method"] and pay_select.find_all("option"):
                            for opt in pay_select.find_all("option"):
                                if opt.get("value"):
                                    form_ctx["payment_method"] = opt.get("value")
                                    break
                    else:
                        pay_radios = form.select("input[type='radio'][name='payment_method'], input[type='radio'][name='payment'], input[type='radio'][name*='pay']")
                        if pay_radios:
                            form_ctx["payment_method"] = self._pick_radio_value(pay_radios, preferred_pay, form)

                if not form_ctx["delivery_method"]:
                    deli_select = form.select_one("select[name='delivery_method'], select[name='delivery'], select[name*='deli'], select[name*='ship']")
                    if deli_select:
                        for opt in deli_select.find_all("option"):
                            v, t = opt.get("value", ""), opt.get_text(strip=True)
                            if preferred_deli and (preferred_deli in t or preferred_deli in v):
                                form_ctx["delivery_method"] = v
                                break
                        if not form_ctx["delivery_method"] and deli_select.find_all("option"):
                            for opt in deli_select.find_all("option"):
                                if opt.get("value"):
                                    form_ctx["delivery_method"] = opt.get("value")
                                    break
                    else:
                        deli_radios = form.select("input[type='radio'][name='delivery_method'], input[type='radio'][name='delivery'], input[type='radio'][name*='deli']")
                        if deli_radios:
                            form_ctx["delivery_method"] = self._pick_radio_value(deli_radios, preferred_deli, form)

                if not form_ctx["delivery_time"]:
                    time_select = form.select_one("select[name='derivery[time]'], select[name='delivery[time]'], select[name='delivery_time'], select[name*='time']")
                    if time_select:
                        form_ctx["time_field_name"] = time_select.get("name", "derivery[time]")
                        for opt in time_select.find_all("option"):
                            t = opt.get_text(strip=True)
                            if "指定なし" in t or "希望なし" in t:
                                form_ctx["delivery_time"] = opt.get("value", "")
                                break
                        else:
                            first_opt = time_select.find("option")
                            if first_opt:
                                form_ctx["delivery_time"] = first_opt.get("value", "")

                if not form_ctx["delivery_date"]:
                    date_select = form.select_one("select[name='derivery[date]'], select[name='delivery[date]'], select[name='delivery_date'], select[name*='date']")
                    if date_select:
                        form_ctx["date_field_name"] = date_select.get("name", "derivery[date]")
                        for opt in date_select.find_all("option"):
                            t = opt.get_text(strip=True)
                            if "指定なし" in t or "希望なし" in t:
                                form_ctx["delivery_date"] = opt.get("value", "")
                                break
                        else:
                            first_opt = date_select.find("option")
                            if first_opt:
                                form_ctx["delivery_date"] = first_opt.get("value", "")

                # Layer 3: Universal Fallback for ANY other unhandled <select> elements in the form
                known_select_names = {
                    "payment_method", "delivery_method",
                    form_ctx.get("time_field_name", ""),
                    form_ctx.get("date_field_name", ""),
                    "address[in][ken]"
                }
                for any_sel in form.find_all("select"):
                    s_name = any_sel.get("name")
                    if s_name and s_name not in known_select_names:
                        sel_opt = any_sel.find("option", selected=True)
                        if sel_opt and sel_opt.get("value") is not None:
                            form_ctx["extra_selects"][s_name] = sel_opt.get("value")
                        else:
                            opts = any_sel.find_all("option")
                            for o in opts:
                                if o.get("value"):
                                    form_ctx["extra_selects"][s_name] = o.get("value")
                                    break

                self.steps_completed = max(self.steps_completed, 2)
                return True, form_ctx, None
            except Exception as e:
                err_desc = str(e) if str(e) else type(e).__name__
                last_error = f"GET /regi 异常: {err_desc}"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
                return False, None, last_error

        return False, None, last_error

    async def submit_regi_info(self, form_context: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str], str]:
        """
        Step 2b: POST buyer details to /regi
        Submits exact customer information, payment method, delivery method, and hidden parameters.
        Returns: (success, confirmation_html, message)
        """
        regi_url = f"{self.base_url}/regi"
        ctx = form_context or {}

        raw_post = self.buyer_info.get("postal_code") or self.buyer_info.get("postcode") or "1000001"
        raw_tel = self.buyer_info.get("tel", "08012345678")
        postcode = unicodedata.normalize("NFKC", str(raw_post)).strip()
        tel = unicodedata.normalize("NFKC", str(raw_tel)).strip()

        # Seed with hidden form fields
        post_data: Dict[str, str] = dict(ctx.get("hidden_fields", {}))

        # Core buyer inputs strictly matching FC2 form (mmcp-nyuryoku.txt)
        post_data.update({
            "address[in][name]": self.buyer_info.get("name", "山田　太郎"),
            "address[in][kkana]": self.buyer_info.get("name_kana") or self.buyer_info.get("kkana") or "ヤマダ　タロウ",
            "address[in][mail]": self.buyer_info.get("email", "buyer_example@gmail.com"),
            "address[in][mail_check]": self.buyer_info.get("email", "buyer_example@gmail.com"),
            "address[in][tel]": tel,
            "address[in][postnum]": postcode,
            "address[in][ken]": self.buyer_info.get("prefecture") or self.buyer_info.get("pref") or "東京都",
            "address[in][address]": self.buyer_info.get("address", "千代田区千代田1-1"),
            "address[in][biko]": self.buyer_info.get("biko", ""),
            "payment_method": (
                ctx.get("payment_method")
                or self.buyer_info.get("payment_method")
                or self.buyer_info.get("preferred_payment")
                or "代金引換"
            ),
            "Submit1": "true"
        })

        # Include delivery method if discovered on page
        if ctx.get("delivery_method"):
            post_data["delivery_method"] = str(ctx["delivery_method"])

        # Include delivery time if discovered on page
        time_name = ctx.get("time_field_name")
        if time_name and "delivery_time" in ctx:
            post_data[time_name] = str(ctx["delivery_time"])

        # Include delivery date if discovered on page
        date_name = ctx.get("date_field_name")
        if date_name and "delivery_date" in ctx:
            post_data[date_name] = str(ctx["delivery_date"])

        # Include any extra unhandled selects from the page
        for s_name, s_val in ctx.get("extra_selects", {}).items():
            if s_name not in post_data:
                post_data[s_name] = str(s_val)

        req_headers = {
            **self.headers,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/regi",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        last_error = ""
        for attempt in range(1, self.retry_count + 1):
            try:
                t0 = time.perf_counter()
                resp = await self.client.post(regi_url, data=post_data, headers=req_headers)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                if resp.status_code != 200:
                    last_error = f"POST /regi HTTP Error {resp.status_code} ({elapsed_ms}ms)"
                    if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                html = resp.text
                # Check for soldout message
                if "カートにある商品が売り切れました" in html or "在庫切れのため" in html:
                    return False, None, f"提交收件人信息时售罄: {elapsed_ms}ms"

                if "カートに商品は入っていません" in html or (resp.url and resp.url.path == "/check"):
                    return False, None, f"提交收件信息失败：购物车已空或会话丢失 ({elapsed_ms}ms)"

                region_html = self._crop_checkout_region(html)
                soup = BeautifulSoup(region_html, "html.parser")
                err_box = soup.select_one(".errormessage, .error")
                if err_box and err_box.get_text(strip=True):
                    return False, None, f"表单校验错误: {err_box.get_text(strip=True)}"

                # Check red <font> validation errors commonly used by FC2 templates
                for font_el in soup.select("#checkout_page font[color*='ff0000'], #checkout_page font[color*='FF0000'], #checkout_page font[color='red'], font[color*='ff0000']"):
                    ftxt = font_el.get_text(strip=True)
                    if ftxt and ftxt != "※" and any(cue in ftxt for cue in ["入力してください", "選択してください", "一致しません", "不正な画面遷移", "正しく", "エラー"]):
                        return False, None, f"表单校验错误: {ftxt}"

                # Verify we actually arrived at the confirmation page (and not a 200 busy/maintenance page or stuck on input form)
                has_confirm_marker = (
                    "ご注文内容の確認" in html
                    or bool(soup.select_one("#confirm_page, #checkout_confirm_page, input[name='Submit2'], input[name='confirm_token'], .btn_submit_final, form[action*='confirm']"))
                )
                still_on_input_form = bool(soup.select_one("input[name='address[in][name]']"))
                if still_on_input_form and not has_confirm_marker:
                    return False, None, f"表单校验未通过：仍停留在收件信息输入页 ({elapsed_ms}ms)"

                if not has_confirm_marker:
                    last_error = f"POST /regi 未到达确认页面(页面繁忙或维护中) ({elapsed_ms}ms)"
                    if attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                self.steps_completed = max(self.steps_completed, 3)
                self._cached_confirm_html = html
                self._cached_confirm_soup = soup
                prefix = f"[重试第{attempt}次成功] " if attempt > 1 else ""
                return True, html, f"{prefix}收件信息提交成功 ({elapsed_ms}ms)"
            except Exception as e:
                err_desc = str(e) if str(e) else type(e).__name__
                last_error = f"POST /regi 网络异常: {err_desc}"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
                return False, None, last_error

        return False, None, last_error

    async def _verify_order_completed_after_timeout(self) -> bool:
        """
        When final POST /regi (Submit2) times out, checks GET /check to see if the session cart
        was emptied by the server completing the order, preventing duplicate live orders!
        """
        try:
            resp = await self.client.get(f"{self.base_url}/check", timeout=3.0)
            if resp.status_code == 200 and "カートに商品は入っていません" in resp.text:
                return True
        except Exception:
            pass
        return False

    async def confirm_final_order(self, confirmation_html: str) -> Dict[str, Any]:
        """
        Step 3: Confirm and finalize order
        Extracts form fields from the confirmation page and sends final POST
        """
        if self._cached_confirm_soup is not None and self._cached_confirm_html == confirmation_html:
            soup = self._cached_confirm_soup
        else:
            soup = BeautifulSoup(self._crop_checkout_region(confirmation_html), "html.parser")
        form = self._select_checkout_form(soup)

        if not form:
            return {
                "success": False,
                "dry_run": self.dry_run,
                "message": "未能定位到确认页面的提交表单 (No confirmation form found)"
            }

        # Check if dry-run
        if self.dry_run:
            self.steps_completed = max(self.steps_completed, 4)
            return {
                "success": True,
                "dry_run": True,
                "message": "[DRY-RUN 模拟测试成功] 已经顺利到达最终订单确认界面！因处于安全模拟模式，未提交最终代金引换订单。",
                "order_id": f"DRYRUN-{int(time.time())}"
            }

        action = form.get("action", "/regi")
        target_url = urljoin(self.base_url, action)

        # Extract input fields carefully, excluding 'back / modify / return' submit buttons and unchecked boxes
        final_payload: Dict[str, str] = {}
        submit_chosen: Optional[Tuple[str, str]] = None
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            val = inp.get("value", "")
            if itype in ("button", "reset", "image"):
                continue
            if itype in ("checkbox", "radio") and "checked" not in inp.attrs:
                continue
            if itype == "submit":
                if name.lower() in ("back", "return", "modify", "cancel") or any(k in val for k in ("修正", "戻る", "キャンセル")):
                    continue
                if submit_chosen is None or name == "Submit2":
                    submit_chosen = (name, val)
                continue
            final_payload[name] = val

        if submit_chosen:
            final_payload[submit_chosen[0]] = submit_chosen[1]
        elif "Submit" not in "".join(final_payload.keys()):
            final_payload["Submit2"] = "true"

        req_headers = {
            **self.headers,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/regi",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        self.in_final_stage = True
        if self.before_final_submit is not None:
            gate_ok = await self.before_final_submit()
            if not gate_ok:
                return {
                    "success": False,
                    "dry_run": False,
                    "aborted_by_gate": True,
                    "message": "已由并发主通道或其它限额线程完成下单，本通道安全中止最终提交以防重单"
                }

        last_error = ""
        for attempt in range(1, self.retry_count + 1):
            try:
                self.final_submitted = True
                t0 = time.perf_counter()
                resp = await self.client.post(target_url, data=final_payload, headers=req_headers)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                if resp.status_code != 200:
                    last_error = f"最终提交 HTTP 错误 {resp.status_code} ({elapsed_ms}ms)"
                    if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return {
                        "success": False,
                        "dry_run": False,
                        "message": last_error
                    }

                res_html = resp.text
                err_soup = BeautifulSoup(self._crop_checkout_region(res_html), "html.parser")
                # Strip all HTML comments so template comments like <!-- 注文完了ページ --> never false-match!
                for c in err_soup.find_all(string=lambda t: isinstance(t, Comment)):
                    c.extract()
                visible_text = err_soup.get_text(" ", strip=True)

                # 1. Check if item sold out at the final step
                if any(k in visible_text for k in ["売り切れました", "在庫切れ", "完売", "品切れ"]):
                    err_el = err_soup.select_one(".errormessage, .error")
                    err_text = err_el.get_text(strip=True) if err_el else "カートにある商品が売り切れました"
                    return {
                        "success": False,
                        "dry_run": False,
                        "is_soldout": True,
                        "message": f"最终提交未完成: {err_text} ({elapsed_ms}ms)"
                    }

                # 2. Check for explicit error messages / session expiration
                err_el = err_soup.select_one(".errormessage, .error")
                if (err_el and err_el.get_text(strip=True)) or "セッションの有効期限" in visible_text or "不正な画面遷移" in visible_text:
                    err_text = err_el.get_text(strip=True) if (err_el and err_el.get_text(strip=True)) else "セッションの有効期限が切れたか、不正な画面遷移が行われました"
                    return {
                        "success": False,
                        "dry_run": False,
                        "is_soldout": False,
                        "message": f"最终提交未完成: {err_text} ({elapsed_ms}ms)"
                    }

                # 3. Look for genuine success markers in visible text (excluding HTML comments)
                success_markers = ["ご注文完了", "注文完了", "ご注文ありがとう", "受付番号", "注文番号"]
                is_success = any(marker in visible_text for marker in success_markers)

                order_no_match = re.search(r"(?:注文番号|受付番号)[：:\s]*([A-Za-z0-9\-]+)", visible_text)
                order_id = order_no_match.group(1) if order_no_match else f"FC2-{int(time.time())}"

                if is_success:
                    self.steps_completed = max(self.steps_completed, 4)
                    return {
                        "success": True,
                        "dry_run": False,
                        "order_id": order_id,
                        "message": f"🎉 下单成功！订单号: {order_id} (耗时: {elapsed_ms}ms)"
                    }

                # If server returned a 200 busy page on final submit, retry within same session
                if ("アクセスが集中" in visible_text or "アクセス集中" in visible_text) and attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue

                return {
                    "success": False,
                    "dry_run": False,
                    "is_soldout": False,
                    "message": f"最终提交未完成: 未知原因，未检测到订单成功标识 ({elapsed_ms}ms)"
                }
            except Exception as e:
                if isinstance(e, httpx.TimeoutException):
                    if await self._verify_order_completed_after_timeout():
                        self.steps_completed = max(self.steps_completed, 4)
                        oid = f"FC2-{int(time.time())}"
                        return {
                            "success": True,
                            "dry_run": False,
                            "order_id": oid,
                            "message": f"🎉 下单成功！订单号: {oid} (最终提交响应超时，但核实购物车已完成结算清空)"
                        }
                last_error = f"最终订单提交网络异常: {e}"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
                return {
                    "success": False,
                    "dry_run": False,
                    "message": last_error
                }

        return {
            "success": False,
            "dry_run": False,
            "message": last_error
        }

    async def execute_checkout(
        self,
        item_id: str,
        item_title: str,
        item_url: Optional[str] = None,
        quantity: int = 1,
        close_on_finish: bool = True
    ) -> Dict[str, Any]:
        """
        Orchestrates full checkout flow:
        1. Add to Cart
        2. Fetch /regi payment method
        3. Submit buyer address & payment
        4. Confirm order (or dry-run simulation)
        """
        start_time = time.perf_counter()
        timeline = []

        # 1. Add to cart
        cart_ok, cart_msg = await self.add_to_cart(item_id, item_url, quantity)
        timeline.append(f"加购: {cart_msg}")
        if not cart_ok:
            if close_on_finish:
                await self.close()
            is_soldout = any(k in cart_msg for k in ["在庫切れ", "売り切れ", "品切れ", "完売", "售罄"])
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": cart_msg,
                "is_soldout": is_soldout,
                "timeline": timeline,
                "elapsed_seconds": round(time.perf_counter() - start_time, 2)
            }

        # 2. Get regi form payment & delivery options
        regi_ok, form_ctx, regi_err = await self.get_regi_form()
        if not regi_ok or not form_ctx:
            timeline.append(f"获取结算表单失败: {regi_err}")
            if close_on_finish:
                await self.close()
            is_soldout = any(k in str(regi_err) for k in ["在庫切れ", "売り切れ", "品切れ", "完売", "售罄"])
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": regi_err,
                "is_soldout": is_soldout,
                "timeline": timeline,
                "elapsed_seconds": round(time.perf_counter() - start_time, 2)
            }
        pay_str = form_ctx.get('payment_method') or '默认'
        deli_str = form_ctx.get('delivery_method') or '默认'
        time_str = form_ctx.get('delivery_time') or '指定なし'
        timeline.append(f"表单识别: 支付={pay_str}, 配送={deli_str}, 时间={time_str}")

        # 3. Submit buyer details
        info_ok, confirm_html, info_msg = await self.submit_regi_info(form_ctx)
        timeline.append(f"提交订单信息: {info_msg}")
        if not info_ok or not confirm_html:
            if close_on_finish:
                await self.close()
            is_soldout = any(k in str(info_msg) for k in ["在庫切れ", "売り切れ", "品切れ", "完売", "售罄"])
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": info_msg,
                "is_soldout": is_soldout,
                "timeline": timeline,
                "elapsed_seconds": round(time.perf_counter() - start_time, 2)
            }

        # 4. Final confirmation / Dry-run
        final_res = await self.confirm_final_order(confirm_html)
        timeline.append(f"订单确认: {final_res.get('message')}")
        if close_on_finish:
            await self.close()

        final_soldout = bool(final_res.get("is_soldout", False)) or any(
            k in str(final_res.get("message", "")) for k in ["在庫切れ", "売り切れ", "品切れ", "完売", "售罄"]
        )
        final_res.update({
            "item_id": item_id,
            "item_title": item_title,
            "is_soldout": final_soldout,
            "timeline": timeline,
            "elapsed_seconds": round(time.perf_counter() - start_time, 2)
        })
        return final_res
