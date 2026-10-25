import re
import time
import asyncio
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup
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
        self.retry_delay = max(0.1, retry_delay)
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

    async def close(self):
        if self.client and not self.client.is_closed:
            await self.client.aclose()

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
        for attempt in range(1, self.retry_count + 1):
            try:
                t0 = time.perf_counter()
                resp = await self.client.post(cart_in_url, data=post_data, headers=req_headers)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)

                if resp.status_code != 200:
                    last_error = f"Cart_in HTTP Error {resp.status_code} ({elapsed_ms}ms)"
                    if resp.status_code in (500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, last_error

                html = resp.text
                # Check for explicit out-of-stock messages
                if any(kw in html for kw in ["在庫切れ", "売り切れ", "完売", "品切れ"]):
                    return False, f"加购失败：商品在庫切れ已售罄 ({elapsed_ms}ms)"

                # Check if cart is empty
                if "カートに商品は入っていません" in html:
                    return False, f"加购未完成：购物车为空或尚未开放 ({elapsed_ms}ms)"

                # Check if checkout button exists
                if "/regi" in html or "btn_submit_checkout" in html or "レジへすすむ" in html:
                    prefix = f"[重试第{attempt}次成功] " if attempt > 1 else ""
                    return True, f"{prefix}加购成功！耗时: {elapsed_ms}ms"

                return True, f"加购已发送 ({elapsed_ms}ms)"
            except Exception as e:
                err_desc = str(e) if str(e) else type(e).__name__
                last_error = f"加购网络异常: {err_desc}"
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay)
                    continue
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
                    if resp.status_code in (500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                html = resp.text
                if "カートにある商品が売り切れました" in html:
                    return False, None, "商品在结算时售罄 (カートにある商品が売り切れました)"

                soup = BeautifulSoup(html, "html.parser")
                form = soup.select_one("form[action*='regi'], #checkout_page form, form[name='form']")
                if not form:
                    form = soup.select_one("form[method='post']")

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

                if form:
                    # 1. Capture all hidden inputs (tokens, session IDs, ticket tokens)
                    for inp in form.find_all("input", type="hidden"):
                        name = inp.get("name")
                        val = inp.get("value", "")
                        if name and name != "Submit1":
                            form_ctx["hidden_fields"][name] = val

                    preferred_pay = self.buyer_info.get("payment_method", "代金引換")
                    preferred_deli = self.buyer_info.get("delivery_method", "宅急便")

                    # Layer 1: Semantic Table Row Inspection (based on label text like '配送方法', 'お支払い方法')
                    for tr in form.find_all("tr"):
                        header_cell = tr.find(["td", "th"])
                        label_text = header_cell.get_text(strip=True) if header_cell else ""
                        sel = tr.find("select")
                        radios = tr.find_all("input", type="radio")

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
                                for r in radios:
                                    if "checked" in r.attrs or (preferred_pay and preferred_pay in r.get("value", "")):
                                        form_ctx["payment_method"] = r.get("value", "")
                                        break
                                if not form_ctx["payment_method"] and radios:
                                    form_ctx["payment_method"] = radios[0].get("value", "")

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
                                for r in radios:
                                    if "checked" in r.attrs or (preferred_deli and preferred_deli in r.get("value", "")):
                                        form_ctx["delivery_method"] = r.get("value", "")
                                        break
                                if not form_ctx["delivery_method"] and radios:
                                    form_ctx["delivery_method"] = radios[0].get("value", "")

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
                            # Pick selected option or first option with value
                            sel_opt = any_sel.find("option", selected=True)
                            if sel_opt and sel_opt.get("value") is not None:
                                form_ctx["extra_selects"][s_name] = sel_opt.get("value")
                            else:
                                opts = any_sel.find_all("option")
                                for o in opts:
                                    if o.get("value"):
                                        form_ctx["extra_selects"][s_name] = o.get("value")
                                        break

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

        import unicodedata
        postcode = unicodedata.normalize('NFKC', str(self.buyer_info.get("postal_code", "1000001"))).strip()
        tel = unicodedata.normalize('NFKC', str(self.buyer_info.get("tel", "08012345678"))).strip()

        # Seed with hidden form fields
        post_data: Dict[str, str] = dict(ctx.get("hidden_fields", {}))

        # Core buyer inputs strictly matching FC2 form (mmcp-nyuryoku.txt)
        post_data.update({
            "address[in][name]": self.buyer_info.get("name", "山田　太郎"),
            "address[in][kkana]": self.buyer_info.get("name_kana", "ヤマダ　タロウ"),
            "address[in][mail]": self.buyer_info.get("email", "buyer_example@gmail.com"),
            "address[in][mail_check]": self.buyer_info.get("email", "buyer_example@gmail.com"),
            "address[in][tel]": tel,
            "address[in][postnum]": postcode,
            "address[in][ken]": self.buyer_info.get("prefecture", "東京都"),
            "address[in][address]": self.buyer_info.get("address", "千代田区千代田1-1"),
            "address[in][biko]": self.buyer_info.get("biko", ""),
            "payment_method": ctx.get("payment_method") or self.buyer_info.get("payment_method", "代金引換"),
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
                    if resp.status_code in (500, 502, 503, 504) and attempt < self.retry_count:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    return False, None, last_error

                html = resp.text
                # Check for error message
                if "カートにある商品が売り切れました" in html:
                    return False, None, f"提交收件人信息时售罄: {elapsed_ms}ms"

                soup = BeautifulSoup(html, "html.parser")
                err_box = soup.select_one(".errormessage, .error")
                if err_box and err_box.get_text(strip=True):
                    return False, None, f"表单校验错误: {err_box.get_text(strip=True)}"

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

    async def confirm_final_order(self, confirmation_html: str) -> Dict[str, Any]:
        """
        Step 3: Confirm and finalize order
        Extracts form fields from the confirmation page and sends final POST
        """
        soup = BeautifulSoup(confirmation_html, "html.parser")
        
        # Check if dry-run
        if self.dry_run:
            return {
                "success": True,
                "dry_run": True,
                "message": "[DRY-RUN 模拟测试成功] 已经顺利到达最终订单确认界面！因处于安全模拟模式，未提交最终代金引换订单。",
                "order_id": f"DRYRUN-{int(time.time())}"
            }

        # Live Mode: Find form to submit
        form = soup.select_one("form[action*='regi'], #checkout_page form, form[name='form']")
        if not form:
            # Fallback to any post form
            form = soup.select_one("form[method='post']")

        if not form:
            return {
                "success": False,
                "dry_run": False,
                "message": "未能定位到确认页面的提交表单 (No confirmation form found)"
            }

        action = form.get("action", "/regi")
        target_url = urljoin(self.base_url, action)

        # Extract all input fields
        final_payload: Dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            val = inp.get("value", "")
            if name:
                # If it's a submit button, include it
                final_payload[name] = val

        # If no submit field was captured, ensure standard Submit flag is present
        if "Submit" not in "".join(final_payload.keys()):
            final_payload["Submit2"] = "true"

        req_headers = {
            **self.headers,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/regi",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            t0 = time.perf_counter()
            resp = await self.client.post(target_url, data=final_payload, headers=req_headers)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            if resp.status_code != 200:
                return {
                    "success": False,
                    "dry_run": False,
                    "message": f"最终提交 HTTP 错误 {resp.status_code} ({elapsed_ms}ms)"
                }

            res_html = resp.text
            # Look for success markers
            success_markers = ["注文完了", "ご注文ありがとう", "受付番号", "order_id", "完了"]
            is_success = any(marker in res_html for marker in success_markers)

            # Try to extract order number if present
            order_no_match = re.search(r"(?:注文番号|受付番号)[：:\s]*([A-Za-z0-9\-]+)", res_html)
            order_id = order_no_match.group(1) if order_no_match else f"FC2-{int(time.time())}"

            if is_success:
                return {
                    "success": True,
                    "dry_run": False,
                    "order_id": order_id,
                    "message": f"🎉 下单成功！订单号: {order_id} (耗时: {elapsed_ms}ms)"
                }
            else:
                # Check for errors in the page
                err_soup = BeautifulSoup(res_html, "html.parser")
                err_el = err_soup.select_one(".errormessage, .error")
                err_text = err_el.get_text(strip=True) if err_el else "未知原因，未检测到订单成功标识"
                return {
                    "success": False,
                    "dry_run": False,
                    "message": f"最终提交未完成: {err_text} ({elapsed_ms}ms)"
                }
        except Exception as e:
            return {
                "success": False,
                "dry_run": False,
                "message": f"最终订单提交网络异常: {e}"
            }

    async def execute_checkout(
        self,
        item_id: str,
        item_title: str,
        item_url: Optional[str] = None,
        quantity: int = 1
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
        await self.close()

        final_res.update({
            "item_id": item_id,
            "item_title": item_title,
            "is_soldout": False,
            "timeline": timeline,
            "elapsed_seconds": round(time.perf_counter() - start_time, 2)
        })
        return final_res
