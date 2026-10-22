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
        dry_run: bool = True
    ):
        self.base_url = base_url.rstrip("/")
        self.buyer_info = buyer_info or {}
        self.dry_run = dry_run
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        # Dedicated client to maintain cookies / session for an order attempt
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
            http2=HAS_HTTP2,
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

        try:
            t0 = time.perf_counter()
            resp = await self.client.post(cart_in_url, data=post_data, headers=req_headers)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            if resp.status_code != 200:
                return False, f"Cart_in HTTP Error {resp.status_code} ({elapsed_ms}ms)"

            html = resp.text
            # Check if cart is empty
            if "カートに商品は入っていません" in html:
                return False, f"加购失败：购物车为空，商品可能已售罄 ({elapsed_ms}ms)"

            # Check if checkout button exists
            if "/regi" in html or "btn_submit_checkout" in html or "レジへすすむ" in html:
                return True, f"加购成功！耗时: {elapsed_ms}ms"

            return True, f"加购已发送 ({elapsed_ms}ms)"
        except Exception as e:
            return False, f"加购网络异常: {e}"

    async def get_regi_form(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Step 2a: GET /regi to inspect the form and fetch available payment methods
        Returns: (success, selected_payment_value, error_message)
        """
        regi_url = f"{self.base_url}/regi"
        try:
            resp = await self.client.get(regi_url)
            if resp.status_code != 200:
                return False, None, f"GET /regi returned HTTP {resp.status_code}"

            html = resp.text
            if "カートにある商品が売り切れました" in html:
                return False, None, "商品在结算时售罄 (カートにある商品が売り切れました)"

            soup = BeautifulSoup(html, "html.parser")
            payment_select = soup.select_one("select[name='payment_method']")
            selected_val = ""

            if payment_select:
                options = payment_select.find_all("option")
                preferred = self.buyer_info.get("payment_method", "代金引換")
                
                # Try finding preferred method
                for opt in options:
                    opt_val = opt.get("value", "")
                    opt_text = opt.get_text(strip=True)
                    if preferred and (preferred in opt_text or preferred in opt_val):
                        selected_val = opt_val
                        break

                # If not matched, pick first option with value
                if not selected_val and options:
                    for opt in options:
                        v = opt.get("value", "")
                        if v:
                            selected_val = v
                            break

            return True, selected_val, None
        except Exception as e:
            return False, None, f"GET /regi 异常: {e}"

    async def submit_regi_info(self, payment_method_val: str) -> Tuple[bool, Optional[str], str]:
        """
        Step 2b: POST buyer details to /regi
        Returns: (success, confirmation_html, message)
        """
        regi_url = f"{self.base_url}/regi"

        post_data = {
            "address[in][name]": self.buyer_info.get("name", "楊　雨晨"),
            "address[in][kkana]": self.buyer_info.get("name_kana", "ヨウウシン"),
            "address[in][mail]": self.buyer_info.get("email", "yyc285184831@gmail.com"),
            "address[in][mail_check]": self.buyer_info.get("email", "yyc285184831@gmail.com"),
            "address[in][tel]": self.buyer_info.get("tel", "08037304421"),
            "address[in][postnum]": self.buyer_info.get("postal_code", "3510015"),
            "address[in][ken]": self.buyer_info.get("prefecture", "埼玉県"),
            "address[in][address]": self.buyer_info.get("address", "朝霞市幸町"),
            "address[in][biko]": self.buyer_info.get("biko", ""),
            "payment_method": payment_method_val,
            "Submit1": "true"
        }

        req_headers = {
            **self.headers,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/regi",
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            t0 = time.perf_counter()
            resp = await self.client.post(regi_url, data=post_data, headers=req_headers)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)

            if resp.status_code != 200:
                return False, None, f"POST /regi HTTP Error {resp.status_code} ({elapsed_ms}ms)"

            html = resp.text
            # Check for error message
            if "カートにある商品が売り切れました" in html:
                return False, None, f"提交收件人信息时售罄: {elapsed_ms}ms"

            soup = BeautifulSoup(html, "html.parser")
            err_box = soup.select_one(".errormessage, .error")
            if err_box and err_box.get_text(strip=True):
                return False, None, f"表单校验错误: {err_box.get_text(strip=True)}"

            return True, html, f"收件信息提交成功 ({elapsed_ms}ms)"
        except Exception as e:
            return False, None, f"POST /regi 网络异常: {e}"

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
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": cart_msg,
                "timeline": timeline,
                "elapsed_seconds": round(time.perf_counter() - start_time, 2)
            }

        # 2. Get regi form payment options
        regi_ok, payment_val, regi_err = await self.get_regi_form()
        if not regi_ok:
            timeline.append(f"获取结算表单失败: {regi_err}")
            await self.close()
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": regi_err,
                "timeline": timeline,
                "elapsed_seconds": round(time.perf_counter() - start_time, 2)
            }
        timeline.append(f"支付方式选择: {payment_val or '默认'}")

        # 3. Submit buyer details
        info_ok, confirm_html, info_msg = await self.submit_regi_info(payment_val or "")
        timeline.append(f"提交订单信息: {info_msg}")
        if not info_ok or not confirm_html:
            await self.close()
            return {
                "success": False,
                "dry_run": self.dry_run,
                "item_id": item_id,
                "item_title": item_title,
                "message": info_msg,
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
            "timeline": timeline,
            "elapsed_seconds": round(time.perf_counter() - start_time, 2)
        })
        return final_res
