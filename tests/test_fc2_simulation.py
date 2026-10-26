"""
===========================================================================
🧪 MAMACHAPP FC2 BOT - 有状态 FC2 模拟服务器 · 全步骤故障注入测试套件
===========================================================================
与 test_resilience.py 不同，本套件不是手动改 bot 的状态位，而是：

  1. 启动一个「有状态」的假 FC2 Cart 服务器 (FakeFC2Server)：
       - 会话 Cookie / 购物车 / 库存 / 订单 / 确认令牌 全部真实建模
       - 页面基于 html-source/ 下的真实模板 (syouhin / mmcp-cart / mmcp-nyuryoku)
       - 可对任意步骤注入：503/502/500/429、200 繁忙页、200 维护页、503 维护页、
         连接失败、读超时(请求未处理)、读超时(服务器已处理但响应丢失)、慢响应、
         会话丢失、页面残缺、服务器先处理后延迟响应 等
  2. 通过 patch httpx.AsyncClient，让 FC2Monitor / FC2Checkout / BotEngine
     内部创建的所有 HTTP 客户端都打到模拟服务器上 —— 被测代码零修改。
  3. 每个用例都以「正确的抢购工具应当具备的行为」为断言：
       ✅ PASS  = 行为正确
       🐞 BUG   = 发现缺陷 / 机制漏洞（附带原因与后果说明）

运行:
    python tests/test_fc2_simulation.py              # 全部
    python tests/test_fc2_simulation.py E02 F06      # 只跑指定用例
    set SIM_VERBOSE=1 → 同时打印机器人内部日志
===========================================================================
"""

import sys
import os
import re
import time
import copy
import asyncio
import itertools
import contextlib
import traceback
import unittest.mock
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx
from bs4 import BeautifulSoup

if sys.platform == "win32":
    import io
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.monitor import FC2Monitor, ShopItem  # noqa: E402
from core.checkout import FC2Checkout  # noqa: E402
from core.config import ConfigManager, DEFAULT_CONFIG  # noqa: E402
from core.bot import BotEngine  # noqa: E402

VERBOSE = os.environ.get("SIM_VERBOSE") == "1"
BASE = "https://mamachapptoy.cart.fc2.com"
_REAL_ASYNC_CLIENT = httpx.AsyncClient

GREEN, RED, YELLOW, CYAN, BOLD, DIM, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[1m", "\033[2m", "\033[0m"
)

# =========================================================================
# 真实 HTML 模板
# =========================================================================
HTML_DIR = PROJECT_ROOT / "html-source"


def _load(name: str) -> str:
    return (HTML_DIR / name).read_text(encoding="utf-8", errors="replace")


TPL_PRODUCT = _load("syouhin.html")
TPL_CART = _load("mmcp-cart.html")
TPL_REGI = _load("mmcp-nyuryoku.html")

TPL_TITLE = "ちっちゃなシリーズ　ちぃち　クロスゴシックドレス（茶髪、ブルードレス）"
TPL_STOCK = "１１月１日夜開始予定"
TPL_STOCK_LI = f'<li class="stock">{TPL_STOCK}</li>'
TPL_CART_BTN = '<input type="submit" value="カートに入れる" class="btn_submit_cart" />'
TPL_EMPTY_CART = "カートに商品は入っていません"
TPL_PAY_SELECT = "<select name='payment_method'></select>"
TPL_SUBMIT1 = '<input type="hidden" name="Submit1" value="true" />'
TPL_REGI_FORM = '<form name="form" method="post" action="/regi">'
_CHECKOUT_DIV_RE = re.compile(r'<div class="contents" id="checkout_page">.*?<br clear="all" />\s*</div>', re.S)

for _marker, _tpl, _name in [
    (TPL_TITLE, TPL_PRODUCT, "syouhin.html"), (TPL_STOCK_LI, TPL_PRODUCT, "syouhin.html"),
    (TPL_CART_BTN, TPL_PRODUCT, "syouhin.html"), (TPL_EMPTY_CART, TPL_CART, "mmcp-cart.html"),
    (TPL_PAY_SELECT, TPL_REGI, "mmcp-nyuryoku.html"), (TPL_SUBMIT1, TPL_REGI, "mmcp-nyuryoku.html"),
    (TPL_REGI_FORM, TPL_REGI, "mmcp-nyuryoku.html"),
]:
    if _marker not in _tpl:
        raise RuntimeError(f"真实模板 {_name} 缺少预期片段: {_marker!r}，请检查 html-source 是否被修改")
if not _CHECKOUT_DIV_RE.search(TPL_REGI):
    raise RuntimeError("mmcp-nyuryoku.html 中找不到 #checkout_page 区块")


def wrap_real(content: str) -> str:
    """把内容嵌入真实 FC2 店铺模板 (含页头搜索表单、侧栏、模板注释)"""
    return _CHECKOUT_DIV_RE.sub(lambda _m: content, TPL_REGI, count=1)


def wrap_min(content: str) -> str:
    return f"<html><head><meta charset='utf-8'><title>ママチャップSHOP</title></head><body>{content}</body></html>"


BUSY_HTML = (
    "<html><head><meta charset='utf-8'><title>アクセス集中</title></head><body>"
    "<h1>只今アクセスが集中しております</h1>"
    "<p>大変混み合っております。しばらく時間をおいてから再度アクセスしてください。</p></body></html>"
)
MAINT_HTML = (
    "<html><head><meta charset='utf-8'><title>メンテナンス</title></head><body>"
    "<div class='maintenance_box'><h1>只今メンテナンス中です</h1>"
    "<p>ご不便をおかけしております。メンテナンス終了まで今しばらくお待ちください。</p></div></body></html>"
)
SESSION_EXPIRED_CONTENT = (
    '<div class="contents" id="checkout_page"><h2>エラー</h2>'
    "<p>セッションの有効期限が切れたか、不正な画面遷移が行われました。お手数ですが最初からやり直してください。</p></div>"
)
SOLDOUT_CARTIN_CONTENT = (
    '<div class="contents" id="checkcart_page"><h2>カートの中身</h2>'
    '<p class="errormessage">申し訳ございません。ご指定の商品は在庫切れのためカートに入れることができません。</p></div>'
)
SOLDOUT_REGI_CONTENT = (
    '<div class="contents" id="checkout_page"><p class="errormessage">カートにある商品が売り切れました</p></div>'
)
EMPTY_CART_MIN = (
    '<div class="contents" id="checkcart_page"><h2>カートの中身</h2><table class="table1"><tr>'
    f'<td colspan="5" class="td3" id="center1">{TPL_EMPTY_CART}</td></tr></table>'
    '<p class="goto_cart"><a href="/regi" class="btn_submit_checkout">レジへすすむ</a></p></div>'
)


# =========================================================================
# 有状态 FC2 模拟服务器
# =========================================================================
@dataclass
class SimItem:
    item_id: str
    title: str
    price: int = 22800
    stock: int = 1
    upcoming: bool = False
    stock_text: Optional[str] = None
    cart_button: bool = True
    description_extra: str = ""

    def display_stock(self) -> str:
        if self.stock_text is not None:
            return self.stock_text
        if self.upcoming:
            return TPL_STOCK
        return "在庫あり" if self.stock > 0 else "在庫切れ"


@dataclass
class SimSession:
    sid: str
    cart: Dict[str, int] = field(default_factory=dict)
    regi_token: Optional[str] = None
    confirm_token: Optional[str] = None


@dataclass
class ReqCtx:
    method: str
    path: str
    form: Dict[str, str]
    session: SimSession
    t: float

    @property
    def item_id(self) -> Optional[str]:
        return self.form.get("item_id")


@dataclass
class Fault:
    method: str
    path: str
    action: str
    arg: Any = None
    times: int = 1          # -1 = 无限
    when: Optional[Callable[[ReqCtx], bool]] = None
    skip: int = 0
    seen: int = 0
    hits: int = 0


class FakeFC2Server:
    def __init__(self, real_templates: bool = False, payment_values: Optional[Dict[str, str]] = None,
                 regi_token: bool = False, error_style: str = "class"):
        self.real = real_templates
        self.payment_values = payment_values
        self.regi_token = regi_token
        self.error_style = error_style          # "class" -> <p class="errormessage">, "font" -> <font color=red>
        self.items: Dict[str, SimItem] = {}
        self.sessions: Dict[str, SimSession] = {}
        self.orders: List[Dict[str, Any]] = []
        self.requests: List[ReqCtx] = []
        self.faults: List[Fault] = []
        self.maintenance = False
        self.maintenance_status = 200
        self.latency = 0.0
        self.final_response_override: Optional[str] = None
        self._seq = itertools.count(1)
        self._t0 = time.perf_counter()
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------- setup API
    def add_item(self, item_id: str, title: Optional[str] = None, **kw) -> SimItem:
        it = SimItem(item_id=str(item_id), title=title or f"ひなちゃん テストドール {item_id}", **kw)
        self.items[it.item_id] = it
        return it

    def inject(self, method: str, path: str, action: str, arg: Any = None, times: int = 1,
               when: Optional[Callable[[ReqCtx], bool]] = None, skip: int = 0) -> Fault:
        f = Fault(method, path, action, arg, times, when, skip)
        self.faults.append(f)
        return f

    def count(self, method: Optional[str] = None, path: Optional[str] = None,
              item_id: Optional[str] = None, pred: Optional[Callable[[ReqCtx], bool]] = None) -> int:
        n = 0
        for r in self.requests:
            if method and r.method != method:
                continue
            if path and r.path != path:
                continue
            if item_id and r.item_id != item_id:
                continue
            if pred and not pred(r):
                continue
            n += 1
        return n

    def cart_of(self, client: httpx.AsyncClient) -> Dict[str, int]:
        sid = client.cookies.get("cart_sid")
        return dict(self.sessions[sid].cart) if sid in self.sessions else {}

    def page(self, content: str) -> str:
        return wrap_real(content) if self.real else wrap_min(content)

    # ------------------------------------------------------------- pages
    def product_page(self, it: SimItem, *, drop_stock_li: bool = False, cart_button: Optional[bool] = None) -> str:
        html = TPL_PRODUCT.replace(TPL_TITLE, it.title)
        html = html.replace('name="item_id" value="343"', f'name="item_id" value="{it.item_id}"')
        html = html.replace("22,800円", f"{it.price:,}円")
        if drop_stock_li:
            html = html.replace(TPL_STOCK_LI, "")
        else:
            html = html.replace(TPL_STOCK_LI, f'<li class="stock">{it.display_stock()}</li>')
        if not (it.cart_button if cart_button is None else cart_button):
            html = html.replace(TPL_CART_BTN, "")
        if it.description_extra:
            html = html.replace("セット内容●ドール本体", it.description_extra + "セット内容●ドール本体")
        return html

    def catalog_page(self) -> str:
        lis = []
        for it in self.items.values():
            btn = TPL_CART_BTN if (it.stock > 0 and not it.upcoming and it.cart_button) else ""
            lis.append(
                f'<li class="item"><dl><dt class="name"><a href="/ca13/{it.item_id}/">{it.title}</a></dt>'
                f'<dd class="price">{it.price:,}円</dd><dd class="stock">{it.display_stock()}</dd>{btn}</dl></li>'
            )
        content = (f'<div class="contents" id="itemlist_page"><ul class="item_list">{"".join(lis)}</ul>'
                   f'<div class="pagerbox"><p>（全商品{len(self.items)}件）</p></div></div>')
        return self.page(content)

    def cart_page(self, sess: SimSession) -> str:
        if not sess.cart:
            return TPL_CART if self.real else wrap_min(EMPTY_CART_MIN)
        rows = "<br />".join(f"{self.items[i].title} × {q}" for i, q in sess.cart.items())
        if self.real:
            return TPL_CART.replace(TPL_EMPTY_CART, rows)
        return wrap_min(
            f'<div class="contents" id="checkcart_page"><h2>カートの中身</h2><table class="table1"><tr><td>{rows}</td></tr></table>'
            '<p class="goto_cart"><a href="/regi" class="btn_submit_checkout">レジへすすむ</a></p></div>'
        )

    def regi_page(self, sess: SimSession, error: str = "") -> str:
        if self.payment_values:
            opts = "".join(f'<option value="{k}">{v}</option>' for k, v in self.payment_values.items())
        else:
            opts = "" if self.real else '<option value="代金引換">代金引換</option><option value="銀行振込">銀行振込</option>'
        token = f'<input type="hidden" name="regi_token" value="{sess.regi_token}" />' if self.regi_token else ""
        err = ""
        if error:
            err = (f'<p class="errormessage">{error}</p>' if self.error_style == "class"
                   else f'<font color="#ff0000"><b>{error}</b></font><br />')
        if self.real:
            html = TPL_REGI.replace(TPL_PAY_SELECT, f"<select name='payment_method'>{opts}</select>")
            html = html.replace(TPL_SUBMIT1, TPL_SUBMIT1 + token)
            return html.replace(TPL_REGI_FORM, err + TPL_REGI_FORM)
        return wrap_min(
            f'<div class="contents" id="checkout_page"><h2>ご注文情報</h2>{err}{TPL_REGI_FORM}'
            '<table class="table1">'
            '<tr><td class="td2">お名前</td><td class="td3"><input name="address[in][name]" type="text" value="" /></td></tr>'
            '<tr><td class="td2">電話番号</td><td class="td3"><input name="address[in][tel]" type="text" value="" /></td></tr>'
            '<tr><td class="td2">ご住所</td><td class="td3"><textarea name="address[in][address]"></textarea></td></tr>'
            f'<tr><td class="td2">お支払い方法</td><td class="td3"><select name="payment_method">{opts}</select></td></tr>'
            '</table><p class="registerbutton"><input type="submit" value="注文内容の確認" class="btn_submit_order" />'
            f'{TPL_SUBMIT1}{token}</p></form></div>'
        )

    def confirm_page(self, sess: SimSession, form: Dict[str, str]) -> str:
        rows = "<br />".join(f"{self.items[i].title} × {q}" for i, q in sess.cart.items())
        return self.page(
            '<div class="contents" id="checkout_page"><h2>ご注文内容の確認</h2><table class="table1">'
            f'<tr><td class="td2">お名前</td><td class="td3">{form.get("address[in][name]", "")}</td></tr>'
            f'<tr><td class="td2">ご注文商品</td><td class="td3">{rows}</td></tr>'
            f'<tr><td class="td2">お支払い方法</td><td class="td3">{form.get("payment_method", "")}</td></tr></table>'
            f'{TPL_REGI_FORM}<input type="hidden" name="confirm_token" value="{sess.confirm_token}" />'
            '<p class="registerbutton"><input type="button" value="修正する" class="btn_submit_return" onclick="history.back()" />'
            '<input type="submit" name="Submit2" value="注文する" class="btn_submit_order" /></p></form></div>'
        )

    def complete_page(self, oid: str) -> str:
        return self.page(
            '<div class="contents" id="checkout_page"><h2>ご注文完了</h2>'
            f"<p>ご注文ありがとうございました。</p><p>注文番号：{oid}</p></div>"
        )

    # ------------------------------------------------------------- transport
    @staticmethod
    def _html(html: str, status: int = 200) -> httpx.Response:
        return httpx.Response(status, html=html)

    @staticmethod
    def _redirect(path: str) -> httpx.Response:
        return httpx.Response(302, headers={"Location": f"{BASE}{path}"})

    @staticmethod
    async def _wait(seconds: float, request: httpx.Request):
        """模拟服务器耗时；若超过客户端 read timeout，则像真实网络一样抛出 ReadTimeout"""
        read = (request.extensions.get("timeout") or {}).get("read")
        if read is not None and seconds > read:
            await asyncio.sleep(read)
            raise httpx.ReadTimeout(f"[SIM] read timeout after {read}s", request=request)
        await asyncio.sleep(seconds)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread() if request.method == "POST" else b""
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
        sid = None
        for part in request.headers.get("cookie", "").split(";"):
            if part.strip().startswith("cart_sid="):
                sid = part.strip()[len("cart_sid="):]
        new_cookie = None
        if sid not in self.sessions:
            sid = f"S{next(self._seq):04d}"
            self.sessions[sid] = SimSession(sid)
            new_cookie = sid
        ctx = ReqCtx(request.method, request.url.path, form, self.sessions[sid], time.perf_counter() - self._t0)
        self.requests.append(ctx)
        resp = await self._dispatch(request, ctx)
        if new_cookie:
            resp.headers["set-cookie"] = f"cart_sid={new_cookie}; Path=/"
        return resp

    def _match_fault(self, ctx: ReqCtx) -> Optional[Fault]:
        for f in self.faults:
            if f.method not in ("*", ctx.method):
                continue
            if f.path != "*" and f.path != ctx.path:
                continue
            if f.when and not f.when(ctx):
                continue
            f.seen += 1
            if f.seen <= f.skip:
                continue
            if 0 <= f.times <= f.hits:
                continue
            f.hits += 1
            return f
        return None

    async def _dispatch(self, request: httpx.Request, ctx: ReqCtx) -> httpx.Response:
        if self.latency:
            await self._wait(self.latency, request)
        f = self._match_fault(ctx)
        if f:
            a = f.action
            if a == "status":
                code, html = f.arg if isinstance(f.arg, tuple) else (f.arg, None)
                return self._html(html or f"<html><body>{code} Service Temporarily Unavailable</body></html>", code)
            if a == "busy200":
                return self._html(BUSY_HTML)
            if a == "maint200":
                return self._html(MAINT_HTML)
            if a == "html":
                return self._html(f.arg)
            if a == "connect_error":
                raise httpx.ConnectError("[SIM] connection refused", request=request)
            if a == "timeout":
                raise httpx.ReadTimeout("[SIM] read timeout (request NOT processed)", request=request)
            if a == "timeout_after":
                self._route(ctx)  # 服务器已执行副作用
                raise httpx.ReadTimeout("[SIM] read timeout (server DID process the request)", request=request)
            if a == "delay":
                await self._wait(f.arg, request)
            if a == "process_then_delay":
                resp = self._route(ctx)
                await self._wait(f.arg, request)
                return resp
            if a == "drop_session":
                ctx.session.cart.clear()
                ctx.session.confirm_token = None
            if a == "hook":
                f.arg(ctx)
        return self._route(ctx)

    def _route(self, ctx: ReqCtx) -> httpx.Response:
        m, p, sess, form = ctx.method, ctx.path, ctx.session, ctx.form
        if self.maintenance and m != "HEAD":
            return self._html(MAINT_HTML, self.maintenance_status)
        if m == "HEAD":
            return httpx.Response(200)
        if p == "/":
            return self._html(self.catalog_page())
        mm = re.match(r"^/ca\d+/(\d+)/?$", p)
        if mm and m == "GET":
            it = self.items.get(mm.group(1))
            return self._html(self.product_page(it)) if it else self._html("<html><body>404</body></html>", 404)
        if p == "/cart_in" and m == "POST":
            it = self.items.get(form.get("item_id", ""))
            if not it or it.upcoming:
                return self._redirect("/check")
            if it.stock <= 0:
                return self._html(self.page(SOLDOUT_CARTIN_CONTENT))
            qty = int(form.get("order_su") or 1)
            sess.cart[it.item_id] = sess.cart.get(it.item_id, 0) + qty
            return self._redirect("/check")
        if p == "/check":
            return self._html(self.cart_page(sess))
        if p == "/regi" and m == "POST" and "back" in form:
            return self._html(self.regi_page(sess))
        if p == "/regi" and m == "POST" and "Submit2" in form:
            return self._final(ctx)
        if p == "/regi" and m == "POST" and "Submit1" in form:
            return self._confirm(ctx)
        if p == "/regi":
            if not sess.cart:
                return self._redirect("/check")
            if any(self.items[i].stock < q for i, q in sess.cart.items()):
                return self._html(self.page(SOLDOUT_REGI_CONTENT))
            sess.regi_token = f"RT{next(self._seq)}"
            return self._html(self.regi_page(sess))
        return self._html("<html><body>404 Not Found</body></html>", 404)

    def _confirm(self, ctx: ReqCtx) -> httpx.Response:
        sess, f = ctx.session, ctx.form
        if not sess.cart:
            return self._redirect("/check")
        errors = []
        required = {
            "address[in][name]": "お名前", "address[in][kkana]": "フリガナ", "address[in][mail]": "メールアドレス",
            "address[in][tel]": "電話番号", "address[in][postnum]": "郵便番号", "address[in][ken]": "都道府県",
            "address[in][address]": "ご住所",
        }
        for k, label in required.items():
            if not f.get(k, "").strip():
                errors.append(f"{label}を入力してください")
        if f.get("address[in][mail]") != f.get("address[in][mail_check]"):
            errors.append("メールアドレス（確認）が一致しません")
        pay = f.get("payment_method", "")
        if (self.payment_values is not None and pay not in self.payment_values) or not pay:
            errors.append("お支払い方法を選択してください")
        if self.regi_token and f.get("regi_token") != sess.regi_token:
            errors.append("不正な画面遷移です。もう一度最初からやり直してください")
        if errors:
            return self._html(self.regi_page(sess, error="<br />".join(errors)))
        sess.confirm_token = f"CT{next(self._seq)}"
        return self._html(self.confirm_page(sess, f))

    def _final(self, ctx: ReqCtx) -> httpx.Response:
        sess, f = ctx.session, ctx.form
        if self.final_response_override is not None:
            return self._html(self.final_response_override)
        if not sess.confirm_token or f.get("confirm_token") != sess.confirm_token:
            return self._html(self.page(SESSION_EXPIRED_CONTENT))
        if not sess.cart:
            return self._redirect("/check")
        if any(self.items[i].stock < q for i, q in sess.cart.items()):
            sess.cart.clear()
            return self._html(self.page(SOLDOUT_REGI_CONTENT))
        for i, q in sess.cart.items():
            self.items[i].stock -= q
        oid = f"MMC-{len(self.orders) + 1:05d}"
        self.orders.append({"order_id": oid, "sid": sess.sid, "items": dict(sess.cart), "t": ctx.t})
        sess.cart.clear()
        sess.confirm_token = None
        return self._html(self.complete_page(oid))


@contextlib.contextmanager
def fc2_network(server: FakeFC2Server):
    """让被测代码内部 new 出来的所有 httpx.AsyncClient 都连到模拟服务器"""
    class SimAsyncClient(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = server.transport
            kwargs.pop("http2", None)
            kwargs.pop("limits", None)
            super().__init__(*args, **kwargs)

    with unittest.mock.patch.object(httpx, "AsyncClient", SimAsyncClient):
        yield server


# =========================================================================
# 测试工具
# =========================================================================
class BugFound(AssertionError):
    pass


def expect(cond: bool, msg: str):
    if not cond:
        raise BugFound(msg)


class SimConfigManager(ConfigManager):
    """纯内存配置：绝不读写用户的 config.json"""

    def __init__(self, overrides: Optional[Dict[str, Dict[str, Any]]] = None):
        self.config_path = Path(os.devnull)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        for sect, vals in (overrides or {}).items():
            self.config.setdefault(sect, {}).update(vals)

    def load(self):
        return self.config

    def save(self) -> bool:
        return True

    def save_data(self, data) -> bool:
        self.config = data
        return True


BASE_ORDER_CFG = {
    "auto_buy": True, "dry_run": True, "max_orders": 1, "session_retries": 2, "session_retry_delay": 0.1,
    "worker_retries": 3, "worker_retry_delay": 0.05, "hedge_requests": False, "max_concurrent_workers": 3,
    "maintenance_direct_worker_retries": 60,
}
BASE_SHOP_CFG = {
    "priority_hedge_enabled": False, "priority_interval_seconds": 0.1, "priority_rush_interval_seconds": 0.1,
    "priority_timeout_seconds": 3.0, "auto_rush_duration_seconds": 5,
}


@contextlib.asynccontextmanager
async def sim_bot(server: FakeFC2Server, order: Optional[Dict[str, Any]] = None, shop: Optional[Dict[str, Any]] = None):
    with fc2_network(server):
        cfg = SimConfigManager({"order": {**BASE_ORDER_CFG, **(order or {})}, "shop": {**BASE_SHOP_CFG, **(shop or {})}})
        bot = BotEngine(cfg)
        bot.is_running = True
        logs: List[str] = []

        def _log(level: str, message: str):
            line = f"[{level.upper()}] {message}"
            logs.append(line)
            if VERBOSE:
                print(f"      {DIM}{line}{RESET}")

        bot.log = _log
        bot._sim_logs = logs
        bot._sim_tasks = []
        try:
            yield bot
        finally:
            bot.is_running = False
            tasks = [t for t in list(bot.active_order_tasks.values()) + list(bot.priority_monitor_tasks.values())
                     + bot._sim_tasks if t and not t.done()]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for sessions in list(bot.prewarmed_checkouts.values()):
                for s in sessions.values():
                    await s.close()
            if bot.monitor.client is not None and not bot.monitor.client.is_closed:
                await bot.monitor.client.aclose()
            await asyncio.sleep(0.05)


def tail(bot: BotEngine, n: int = 6) -> str:
    lines = bot._sim_logs[-n:]
    return ("\n        机器人日志(末尾):\n          " + "\n          ".join(l[:160] for l in lines)) if lines else ""


def make_item(item_id: str, title: Optional[str] = None) -> ShopItem:
    return ShopItem(item_id=item_id, title=title or f"ひなちゃん テストドール {item_id}", price="22,800円",
                    url=f"{BASE}/ca13/{item_id}/", image_url="", in_stock=True, stock_text="在庫あり", category_id="13")


async def run_worker(bot: BotEngine, item: ShopItem, event: str = "IN_STOCK", timeout: float = 15.0):
    bot.dispatch_order_worker(item, event_type=event)
    task = bot.active_order_tasks.get(item.item_id)
    if task:
        bot._sim_tasks.append(task)
        await asyncio.wait({task}, timeout=timeout)
    return task


def start_priority_monitor(bot: BotEngine, item_id: str, title: Optional[str] = None) -> asyncio.Task:
    d = {"item_id": item_id, "title": title or f"ひなちゃん テストドール {item_id}", "url": f"{BASE}/ca13/{item_id}/",
         "in_stock": False, "is_upcoming": True, "price": "22,800円"}
    bot.upcoming_items[item_id] = d
    t = asyncio.create_task(bot._monitor_single_priority_item(d))
    bot._sim_tasks.append(t)
    return t


async def wait_until(pred: Callable[[], bool], timeout: float, interval: float = 0.05) -> bool:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def new_checkout(dry_run: bool = True, retry_count: int = 3, buyer: Optional[Dict[str, Any]] = None) -> FC2Checkout:
    return FC2Checkout(base_url=BASE, buyer_info=buyer or copy.deepcopy(DEFAULT_CONFIG["buyer"]),
                       dry_run=dry_run, retry_count=retry_count, retry_delay=0.1)


async def make_monitor() -> FC2Monitor:
    return FC2Monitor(BASE)


async def close_monitor(mon: FC2Monitor):
    if mon.client is not None and not mon.client.is_closed:
        await mon.client.aclose()


# =========================================================================
# 用例注册
# =========================================================================
@dataclass
class Case:
    cid: str
    group: str
    title: str
    severity: str
    fn: Callable
    timeout: float = 40.0


CASES: List[Case] = []


def case(cid: str, group: str, title: str, severity: str, timeout: float = 40.0):
    def deco(fn):
        CASES.append(Case(cid, group, title, severity, fn, timeout))
        return fn
    return deco


# =========================================================================
# A. 整站级：打不开 / 繁忙 / 维护
# =========================================================================
@case("A01", "整站", "整站无法连接 (ConnectError)：巡检与详情探测不崩溃、不误判维护", "基线")
async def a01():
    srv = FakeFC2Server()
    srv.add_item("501", stock=3)
    srv.inject("*", "*", "connect_error", times=-1)
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            items, _, maint, _ = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1)
            item, is_m = await mon.check_item_detail(f"{BASE}/ca13/501/", timeout=2.0)
        finally:
            await close_monitor(mon)
    expect(items == [] and not maint, f"连接失败时 check_once 返回异常: items={items}, maint={maint}")
    expect(item is None and not is_m, f"连接失败时 check_item_detail 返回异常: {item}, maint={is_m}")


@case("A02", "整站", "整站 503 繁忙：不误判维护、不累计空页面计数", "基线")
async def a02():
    srv = FakeFC2Server()
    srv.add_item("501", stock=3)
    srv.inject("GET", "*", "status", (503, BUSY_HTML), times=-1)
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            _, _, maint, _ = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=2)
        finally:
            await close_monitor(mon)
    expect(not maint, "503 繁忙被误判为维护")
    expect(mon.empty_catalog_consecutive == 0, f"503 繁忙被计入空页面熔断: {mon.empty_catalog_consecutive}")


@case("A03", "整站", "整站维护页 (HTTP 200)：货架与详情页均识别为维护", "基线")
async def a03():
    srv = FakeFC2Server()
    srv.add_item("501", stock=3)
    srv.maintenance = True
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            _, _, maint, _ = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=2)
            item, is_m = await mon.check_item_detail(f"{BASE}/ca13/501/", timeout=2.0)
        finally:
            await close_monitor(mon)
    expect(maint, "HTTP 200 维护页未被货架巡检识别")
    expect(is_m and item is None, "HTTP 200 维护页未被详情探测识别")


@case("A04", "整站", "整站维护页以 HTTP 503 返回：应识别为维护", "中(取决于FC2实际状态码)")
async def a04():
    srv = FakeFC2Server()
    srv.add_item("501", stock=3)
    srv.maintenance = True
    srv.maintenance_status = 503
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            _, _, maint, _ = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1)
            _, is_m = await mon.check_item_detail(f"{BASE}/ca13/501/", timeout=2.0)
        finally:
            await close_monitor(mon)
    expect(maint or is_m,
           "页面正文明确写着『只今メンテナンス中です』，但因为 HTTP 503，check_maintenance() 第一行就按"
           "『临时繁忙』返回 False。\n        后果: 如果 FC2 用 503 返回维护页（很多站点都这么做），"
           "机器人永远进不了维护状态，维护结束瞬间的『直接加购冲刺』(trigger_maintenance_direct_orders) 永远不会触发。")


@case("A05", "整站", "在售商品详情页描述含『準備中』字样：不应被当成维护", "高")
async def a05():
    srv = FakeFC2Server()
    srv.add_item("502", stock=3, description_extra="※次回生産分のご予約は現在準備中です。")
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            item, is_m = await mon.check_item_detail(f"{BASE}/ca13/502/", timeout=2.0)
        finally:
            await close_monitor(mon)
    expect(item is not None and item.in_stock and not is_m,
           f"在售商品（有购物车按钮、在庫あり）因为描述里有『準備中』被判为维护: item={item}, is_maint={is_m}\n"
           "        原因: MAINTENANCE_KEYWORDS 里的『準備中』匹配的是整页 HTML（含商品描述/meta），不是维护提示区。\n"
           "        后果: 专属监控对这个商品永远拿不到 target_item → 永远不派发下单线程，同时把全局 is_in_maintenance 置 True。")


@case("A06", "整站", "货架上某个未开售商品库存文案为『準備中』：其他在售商品不应被整页判为维护", "高")
async def a06():
    srv = FakeFC2Server()
    srv.add_item("503", stock=3)
    srv.add_item("504", upcoming=True, stock_text="準備中")
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            items, _, maint, reason = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1)
        finally:
            await close_monitor(mon)
    instock = [i.item_id for i in items if i.in_stock]
    expect(not maint and "503" in instock,
           f"货架里只有一个商品的库存文字是『準備中』，整个货架就被判为维护 (maint={maint}, reason={reason})，"
           f"在售商品 503 被忽略 (解析到的在售商品={instock})。\n"
           "        FC2 店铺常用『準備中』作为未开售商品的库存显示文字，这会让货架巡检永久卡在『维护中』。")


@case("A07", "整站", "详情页持续 503 时，对冲探测的请求量应受控 (单次探测 ≤10 个请求)", "中(风控/封IP风险)")
async def a07():
    srv = FakeFC2Server()
    srv.add_item("505", stock=3)
    srv.inject("GET", "/ca13/505/", "status", (503, BUSY_HTML), times=-1)
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            t0 = time.perf_counter()
            await mon.check_item_detail(f"{BASE}/ca13/505/", timeout=4.0, hedge_delay=1.5, hedge_timeout=2.0, enable_hedge=True)
            el = time.perf_counter() - t0
        finally:
            await close_monitor(mon)
    n = srv.count("GET", "/ca13/505/")
    expect(n <= 10,
           f"用默认参数做一次 check_item_detail，{el:.1f}s 内向同一个详情页发了 {n} 个请求 (≈{n / max(el, 0.01):.0f} req/s)。\n"
           "        原因: fetch_page_hedged 里主探针一拿到 503 就立刻重建，对冲探针每次失败只 sleep 0.05s 就再发，没有退避。\n"
           "        后果: 专属监控每 0.3s 调用一次，每个重点商品每秒能发几十个请求；有多个重点商品时容易被 FC2 限流或封 IP，"
           "还会加重服务器拥堵，反过来拖慢自己的下单请求。")


@case("A08", "整站", "货架连续空页面 → 判为隐式维护 → 恢复后解除", "基线")
async def a08():
    srv = FakeFC2Server(real_templates=True)
    srv.add_item("506", stock=3)
    empty = wrap_real('<div class="contents" id="itemlist_page"><p>現在、表示できる商品はございません。</p></div>')
    srv.inject("GET", "/", "html", empty, times=2)
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            r1 = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1, empty_catalog_threshold=2)
            r2 = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1, empty_catalog_threshold=2)
            r3 = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1, empty_catalog_threshold=2)
        finally:
            await close_monitor(mon)
    expect(not r1[2] and r2[2], f"连续 2 次空页面未判为隐式维护: 第1次={r1[2]} 第2次={r2[2]}")
    expect(not r3[2] and len(r3[0]) == 1 and mon.empty_catalog_consecutive == 0, f"商品恢复后未解除: {r3[2]}, {r3[0]}")


@case("A09", "整站", "维护期间货架巡检遇一次 503/网络超时：不能误判为『维护结束！』并提前直发加购", "高")
async def a09():
    srv = FakeFC2Server()
    srv.add_item("507", stock=3, upcoming=True)
    srv.maintenance = True
    async with sim_bot(srv, shop={"interval_seconds": 0.1, "rush_interval_seconds": 0.1, "scan_pages": 1}) as bot:
        bot.upcoming_items["507"] = {"item_id": "507", "title": "ドール507", "url": f"{BASE}/ca13/507/"}
        t = asyncio.create_task(bot._catalog_monitor_loop())
        bot._sim_tasks.append(t)
        await wait_until(lambda: bot.is_in_maintenance, 1.0)
        # 维护期间偶发一次 503 网关繁忙
        srv.inject("GET", "/", "status", 503, times=1)
        await wait_until(lambda: srv.count("GET", "/") >= 2, 1.5)
        await asyncio.sleep(0.15)
        premature_fired = any("【维护结束！新品开售！】" in l for l in bot._sim_logs) or srv.count("POST", "/cart_in") > 0
        expect(bot.is_in_maintenance and not premature_fired,
               f"维护期间仅出现 1 次 HTTP 503，大盘就误报『【维护结束！新品开售！】』并提前直发加购 "
               f"(cart_in次数={srv.count('POST', '/cart_in')})。\n"
               "        原因: check_once 在 503/超时时返回 (items=[], triggers=[], is_maintenance=False)；"
               "_catalog_monitor_loop 只看 if is_maintenance: ... else: if self.is_in_maintenance:，没检查扫描是否真实拿到有效货架响应。\n"
               "        后果: 关店维护期间只要网关抖动一次，就会误发开售通知并把直发加购轮次提前打空在维护页上。" + tail(bot))


@case("A10", "整站", "在售商品库存栏带发货时间说明 (如『在庫あり（11月中旬発送予定）』)：不能因『予定』被判为未开售", "高")
async def a10():
    srv = FakeFC2Server()
    srv.add_item("508", stock=3, stock_text="在庫あり（11月中旬発送予定）", cart_button=True)
    with fc2_network(srv):
        mon = await make_monitor()
        try:
            item, is_m = await mon.check_item_detail(f"{BASE}/ca13/508/", timeout=2.0)
            cat_items, _, _, _ = await mon.check_once(f"{BASE}/?sort=5", [], [], scan_pages=1)
        finally:
            await close_monitor(mon)
    cat_item = cat_items[0] if cat_items else None
    expect(item is not None and item.in_stock and not item.is_upcoming and cat_item and cat_item.in_stock,
           f"明明写着『在庫あり』且有购物车按钮，解析结果却是 in_stock={item and item.in_stock}, is_upcoming={item and item.is_upcoming}。\n"
           "        原因: UPCOMING_KEYWORDS 包含宽泛的二字词『予定』，且 if is_upcoming 分支排在 has_instock_kw 之前，强制把 in_stock 置为 False。\n"
           "        后果: 凡是库存栏注明『○○発送予定』『順次発送予定』的在售/预约商品，机器人都当成未开售，永远不会下单。")


# =========================================================================
# B. 步骤1：加入购物车 POST /cart_in
# =========================================================================
@case("B01", "加购", "加购遇 503→502 后第3次成功，购物车里确实有 1 件", "基线")
async def b01():
    srv = FakeFC2Server()
    srv.add_item("601", stock=3)
    srv.inject("POST", "/cart_in", "status", 503)
    srv.inject("POST", "/cart_in", "status", 502)
    with fc2_network(srv):
        chk = new_checkout()
        try:
            ok, msg = await chk.add_to_cart("601")
            cart = srv.cart_of(chk.client)
        finally:
            await chk.close()
    expect(ok and cart == {"601": 1}, f"ok={ok}, msg={msg}, 服务器购物车={cart}")


@case("B02", "加购", "加购返回 200『アクセス集中』繁忙页：不能当成加购成功", "高")
async def b02():
    srv = FakeFC2Server()
    srv.add_item("602", stock=3)
    srv.inject("POST", "/cart_in", "busy200")
    with fc2_network(srv):
        chk = new_checkout()
        try:
            ok, msg = await chk.add_to_cart("602")
            cart = srv.cart_of(chk.client)
        finally:
            await chk.close()
    expect(not ok or cart.get("602"),
           f"服务器只返回了 200 繁忙页、购物车是空的 ({cart})，add_to_cart 却返回 ok=True『{msg}』，也没有重试。\n"
           "        原因: 页面里没有售罄词、没有『カートに商品は入っていません』、也没有 /regi，就直接落到兜底的 return True『加购已发送』。\n"
           "        后果: 本该在同一个会话里马上重发加购，结果直接进入结算步骤 → 后续步骤空跑，白白浪费开售后最宝贵的几秒。")


@case("B03", "加购", "加购返回 200 维护页：不能当成加购成功", "高")
async def b03():
    srv = FakeFC2Server()
    srv.add_item("602", stock=3)
    srv.inject("POST", "/cart_in", "maint200")
    with fc2_network(srv):
        chk = new_checkout()
        try:
            ok, msg = await chk.add_to_cart("602")
            cart = srv.cart_of(chk.client)
        finally:
            await chk.close()
    expect(not ok or cart.get("602"),
           f"加购返回的是维护页，购物车为空 ({cart})，add_to_cart 却返回 ok=True『{msg}』。")


@case("B04", "加购", "商品名含『完売』(如『完売御礼・再販』)：加购成功后不能被误判售罄", "高")
async def b04():
    srv = FakeFC2Server(real_templates=True)
    srv.add_item("603", title="【完売御礼・再販】ひなちゃん セーラー服", stock=3)
    with fc2_network(srv):
        chk = new_checkout(dry_run=True)
        res = await chk.execute_checkout("603", "【完売御礼・再販】ひなちゃん セーラー服")
    expect(not res.get("is_soldout") and res.get("success"),
           f"商品其实已经加进购物车，但结果是 success={res.get('success')}, is_soldout={res.get('is_soldout')}, msg={res.get('message')}\n"
           "        原因: add_to_cart 拿『在庫切れ/売り切れ/完売/品切れ』去匹配整个购物车页面，而购物车页面会显示商品名。\n"
           "        后果: 机器人把商品永久标记为售罄 (soldout_item_ids)，关闭专属监控和预热连接，再也不会去抢。")


@case("B05", "加购", "加购请求服务器已处理但响应超时 → 会话内重试不能导致购物车数量翻倍", "中(风险)")
async def b05():
    srv = FakeFC2Server()
    srv.add_item("604", stock=5)
    srv.inject("POST", "/cart_in", "timeout_after")
    with fc2_network(srv):
        chk = new_checkout(dry_run=False)
        await chk.warm_up()  # 与机器人预热会话一致：先持有 Cookie
        res = await chk.execute_checkout("604", "テスト")
    qty = srv.orders[0]["items"].get("604") if srv.orders else None
    expect(len(srv.orders) == 1 and qty == 1,
           f"最终订单数量={qty} (订单数={len(srv.orders)}, 结果={res.get('message')})\n"
           "        原因: 第一次 POST /cart_in 服务器已经入车，只是响应超时；重试时又 POST 了一次，order_su 被累加，"
           "而 execute_checkout 从不核对购物车里的数量。\n"
           "        后果: 实盘可能下出 2 件（代引订单要多付钱），或者因为超过限购被 FC2 拒单。")


@case("B06", "加购", "真售罄 (在庫切れ)：识别为 is_soldout 并立即止损", "基线")
async def b06():
    srv = FakeFC2Server()
    srv.add_item("605", stock=0)
    with fc2_network(srv):
        res = await new_checkout().execute_checkout("605", "テスト")
    expect(res.get("is_soldout") is True and not res.get("success"), f"真售罄未被识别: {res}")


@case("B07", "加购", "加购连接失败一次后重试成功", "基线")
async def b07():
    srv = FakeFC2Server()
    srv.add_item("606", stock=3)
    srv.inject("POST", "/cart_in", "connect_error")
    with fc2_network(srv):
        chk = new_checkout()
        try:
            ok, msg = await chk.add_to_cart("606")
            cart = srv.cart_of(chk.client)
        finally:
            await chk.close()
    expect(ok and cart == {"606": 1}, f"ok={ok}, msg={msg}, cart={cart}")


# =========================================================================
# C. 步骤2：结算表单 GET /regi
# =========================================================================
@case("C01", "结算表单", "GET /regi 连续 503×2 后恢复，正确识别支付方式", "基线")
async def c01():
    srv = FakeFC2Server()
    srv.add_item("611", stock=3)
    srv.inject("GET", "/regi", "status", 503, times=2)
    with fc2_network(srv):
        chk = new_checkout()
        try:
            await chk.add_to_cart("611")
            ok, ctx, err = await chk.get_regi_form()
        finally:
            await chk.close()
    expect(ok and ctx and ctx["payment_method"] == "代金引換", f"ok={ok}, ctx={ctx}, err={err}")


@case("C02", "结算表单", "GET /regi 返回 200 繁忙页：应重试，不能把繁忙页当表单解析", "中")
async def c02():
    srv = FakeFC2Server(payment_values={"1": "代金引換", "2": "銀行振込"})
    srv.add_item("612", stock=3)
    srv.inject("GET", "/regi", "busy200")
    with fc2_network(srv):
        chk = new_checkout()
        try:
            await chk.add_to_cart("612")
            ok, ctx, err = await chk.get_regi_form()
        finally:
            await chk.close()
    expect(not ok or (ctx and ctx.get("payment_method") == "1"),
           f"get_regi_form 把繁忙页当成功返回: ok={ok}, payment_method={ctx and ctx.get('payment_method')!r}, hidden={ctx and ctx.get('hidden_fields')}\n"
           "        原因: 只要 HTTP 200 就 return True，不检查页面里到底有没有 /regi 表单。\n"
           "        后果: 拿不到隐藏字段和支付方式的 option 值，下一步提交大概率被 FC2 校验打回，整单失败。")


@case("C03", "结算表单", "真实 nyuryoku.html：表单定位必须命中 /regi 表单而不是页头搜索框", "高")
async def c03():
    srv = FakeFC2Server(real_templates=True, payment_values={"1": "代金引換", "2": "銀行振込"}, regi_token=True)
    srv.add_item("613", stock=3)
    with fc2_network(srv):
        chk = new_checkout()
        try:
            await chk.add_to_cart("613")
            ok, ctx, err = await chk.get_regi_form()
            regi_html = (await chk.client.get(f"{BASE}/regi")).text
        finally:
            await chk.close()
    picked = BeautifulSoup(regi_html, "html.parser").select_one("form[action*='regi'], #checkout_page form, form[name='form']")
    picked_desc = f"<form action='{picked.get('action')}' method='{picked.get('method')}'>" if picked else None
    expect(ok and ctx and "regi_token" in ctx["hidden_fields"] and ctx["payment_method"] == "1",
           f"选择器选中的是 {picked_desc} → hidden_fields={ctx and ctx['hidden_fields']}, payment_method={ctx and ctx['payment_method']!r}\n"
           "        原因: BeautifulSoup.select_one('A, B, C') 按『文档顺序』返回第一个匹配的元素，不是按选择器顺序；"
           "真实模板的页头搜索框 <form name=\"form\" action=\"/\"> 排在前面，于是被 form[name='form'] 先命中。\n"
           "        后果: /regi 表单里的隐藏令牌和 option 真实值全部丢失；如果 FC2 用令牌或 ID 型支付值，第3步必然被拒。")


@case("C04", "结算表单", "加购后到结算时被别人买光 (カートにある商品が売り切れました) → is_soldout", "基线")
async def c04():
    srv = FakeFC2Server()
    srv.add_item("614", stock=3)
    srv.inject("GET", "/regi", "hook", lambda c: setattr(srv.items["614"], "stock", 0))
    with fc2_network(srv):
        res = await new_checkout().execute_checkout("614", "テスト")
    expect(res.get("is_soldout") is True and not res.get("success"), f"结算时售罄未识别: {res.get('message')}")


@case("C05", "结算表单", "加购后会话丢失 (购物车被清空)：模拟下单不能报告成功", "中(模拟结果失真)")
async def c05():
    srv = FakeFC2Server()
    srv.add_item("615", stock=3)
    srv.inject("GET", "/regi", "drop_session")
    with fc2_network(srv):
        res = await new_checkout(dry_run=True).execute_checkout("615", "テスト")
    expect(not res.get("success"),
           f"购物车已经空了，服务器从没出过确认页，DRY-RUN 却报告『{res.get('message')}』\n"
           f"        时间线: {res.get('timeline')}\n"
           "        原因: get_regi_form / submit_regi_info 只要拿到 200 就算成功，不核对返回的是不是确认页；"
           "confirm_final_order 在 dry_run 时不检查任何内容就直接 success。\n"
           "        后果: 模拟测试显示『跑通了』，并不能证明实盘能下单，模拟结果不可信。")


@case("C06", "结算表单", "支付方式为单选框 <input type='radio'> 且文案在标签文本中：应选中用户配置的『代金引換』", "高")
async def c06():
    srv = FakeFC2Server()
    srv.add_item("616", stock=3)
    radio_regi_html = wrap_min(
        '<div class="contents" id="checkout_page"><h2>ご注文情報</h2>'
        '<form name="form" method="post" action="/regi"><table class="table1">'
        '<tr><td class="td2">お支払い方法</td><td class="td3">'
        '<label><input type="radio" name="payment_method" value="1" checked="checked" /> 銀行振込</label>'
        '<label><input type="radio" name="payment_method" value="2" /> 代金引換</label>'
        '</td></tr></table><input type="hidden" name="Submit1" value="true" /></form></div>'
    )
    srv.inject("GET", "/regi", "html", radio_regi_html)
    with fc2_network(srv):
        chk = new_checkout()
        try:
            await chk.add_to_cart("616")
            ok, ctx, err = await chk.get_regi_form()
        finally:
            await chk.close()
    expect(ok and ctx and ctx.get("payment_method") == "2",
           f"用户配置 preferred_pay='代金引換' (对应 value='2')，但 get_regi_form 选中的是 payment_method={ctx and ctx.get('payment_method')!r} (銀行振込)。\n"
           "        原因: radio 分支只检查 'checked' in r.attrs or preferred_pay in r.get('value', '')，既优先命中了默认 checked 的第一项，"
           "又没有读取 radio 旁边的 label 文本，且 Layer 2 fallback 完全忽略了 radio。\n"
           "        后果: 当店铺用 radio 选择支付/配送方式且 value 为数字编码时，机器人会选错支付方式或校验失败。")


# =========================================================================
# D. 步骤3：提交收件信息 POST /regi (Submit1)
# =========================================================================
@case("D01", "提交信息", "POST /regi 遇 503 后重试成功并到达确认页", "基线")
async def d01():
    srv = FakeFC2Server()
    srv.add_item("621", stock=3)
    srv.inject("POST", "/regi", "status", 503, when=lambda c: "Submit1" in c.form)
    with fc2_network(srv):
        chk = new_checkout()
        try:
            await chk.add_to_cart("621")
            _, ctx, _ = await chk.get_regi_form()
            ok, html, msg = await chk.submit_regi_info(ctx)
        finally:
            await chk.close()
    expect(ok and html and "Submit2" in html, f"ok={ok}, msg={msg}")


@case("D02", "提交信息", "表单校验错误用红字<font>显示 (非 .error 类)：不能当作提交成功", "中")
async def d02():
    srv = FakeFC2Server(error_style="font")
    srv.add_item("622", stock=3)
    buyer = copy.deepcopy(DEFAULT_CONFIG["buyer"])
    buyer["tel"] = ""  # 漏填电话 → FC2 校验报错
    with fc2_network(srv):
        res = await new_checkout(dry_run=True, buyer=buyer).execute_checkout("622", "テスト")
    expect(not res.get("success"),
           f"服务器返回的是带红字错误『電話番号を入力してください』的输入页，结果却是 success={res.get('success')}: {res.get('message')}\n"
           "        原因: submit_regi_info 只认 .errormessage/.error 这两个 class；FC2 模板一般用 <font color=#ff0000> 显示错误。\n"
           "        后果: 买家信息填错时，模拟模式照样显示成功，用户上线实盘后才发现每单都失败。")


@case("D03", "提交信息", "POST /regi 返回 200 繁忙页：应重试，不能当作已到达确认页", "中")
async def d03():
    srv = FakeFC2Server()
    srv.add_item("623", stock=3)
    srv.inject("POST", "/regi", "busy200", when=lambda c: "Submit1" in c.form)
    with fc2_network(srv):
        chk = new_checkout(dry_run=True)
        res = await chk.execute_checkout("623", "テスト")
    issued = any(s.confirm_token for s in srv.sessions.values()) or srv.count("POST", "/regi", pred=lambda c: "Submit1" in c.form) > 1
    expect(not res.get("success") or issued,
           f"POST /regi 只拿到繁忙页，没有重试也没有到确认页，结果却是『{res.get('message')}』\n"
           "        后果: 实盘时会因为找不到确认表单而失败，但本来在同一个会话里重试一次就能过。")


# =========================================================================
# E. 步骤4：最终确认 POST /regi (Submit2)
# =========================================================================
@case("E01", "最终确认", "[实盘·极简页面] 标准全流程能真实下单 1 次，订单号正确", "基线")
async def e01():
    srv = FakeFC2Server()
    srv.add_item("631", stock=3)
    with fc2_network(srv):
        res = await new_checkout(dry_run=False).execute_checkout("631", "テスト")
    expect(res.get("success") and len(srv.orders) == 1 and res.get("order_id") == srv.orders[0]["order_id"],
           f"res={res.get('message')}, orders={srv.orders}")


@case("E02", "最终确认", "[实盘·真实模板] 确认页带页头搜索框：最终提交必须真正出单", "致命")
async def e02():
    srv = FakeFC2Server(real_templates=True)
    srv.add_item("632", stock=3)
    with fc2_network(srv):
        res = await new_checkout(dry_run=False).execute_checkout("632", "テスト")
    posts = [f"{r.method} {r.path} {sorted(r.form.keys())}" for r in srv.requests if r.method == "POST"]
    expect(len(srv.orders) == 1,
           f"服务器上的订单数={len(srv.orders)}，机器人却报告 success={res.get('success')}『{res.get('message')}』\n"
           f"        机器人发出的 POST 请求: {posts}\n"
           "        原因1: confirm_final_order 的 select_one 按文档顺序先命中页头搜索框 <form name='form' action='/'>，"
           "最终提交被发到了『/』(店铺首页)，带的参数只有 word=''。\n"
           "        原因2: 成功判定词里有『完了』，而 FC2 每个页面的模板里都带注释 <!-- 注文完了ページ -->，"
           "所以首页也被判成『下单成功』。\n"
           "        后果: 实盘时实际没有下单，机器人却把商品记为已购、推送『🎉 抢购成功』，并调用 stop() 停止所有监控。")


@case("E03", "最终确认", "最终提交返回模板错误页 (会话过期)：不能判为下单成功", "致命")
async def e03():
    srv = FakeFC2Server()
    srv.add_item("633", stock=3)
    srv.final_response_override = wrap_real(SESSION_EXPIRED_CONTENT)
    with fc2_network(srv):
        res = await new_checkout(dry_run=False).execute_checkout("633", "テスト")
    expect(not res.get("success"),
           f"返回的是『セッションの有効期限が切れました』错误页，没有生成任何订单 (orders={len(srv.orders)})，"
           f"机器人却报告: {res.get('message')}\n"
           "        原因: success_markers 里的『完了』/『注文完了』能匹配到模板里的 HTML 注释 <!-- 注文完了ページ -->；"
           "『order_id』这个英文词也很容易误匹配。")


@case("E04", "最终确认", "最终提交遇一次 503：应在同一会话内重试，不丢失已到手的购物车", "中")
async def e04():
    srv = FakeFC2Server()
    srv.add_item("634", stock=3)
    srv.inject("POST", "/regi", "status", 503, when=lambda c: "Submit2" in c.form)
    with fc2_network(srv):
        res = await new_checkout(dry_run=False).execute_checkout("634", "テスト")
    expect(res.get("success") and len(srv.orders) == 1,
           f"最终提交只遇到一次 503 就整单失败: {res.get('message')}\n"
           "        原因: confirm_final_order 完全没有重试，失败后 execute_checkout 直接关闭会话。\n"
           "        后果: 抢到手的购物车被丢掉，worker 只能用新会话从加购重新开始；抢购高峰时这一轮基本就没了。")


@case("E05", "最终确认", "最终提交服务器已出单但响应超时 → worker 重试不能重复下单", "致命")
async def e05():
    srv = FakeFC2Server()
    srv.add_item("635", stock=5)
    srv.inject("POST", "/regi", "timeout_after", when=lambda c: "Submit2" in c.form)
    async with sim_bot(srv, order={"dry_run": False, "worker_retries": 3}) as bot:
        await run_worker(bot, make_item("635"))
        expect(len(srv.orders) == 1,
               f"服务器上生成了 {len(srv.orders)} 个订单 {[o['order_id'] for o in srv.orders]}\n"
               "        原因: 最终提交的读超时被当作普通失败，worker 换新会话从头再买一次，没有任何『订单可能已成立』的判断"
               "（比如去查订单完成页或邮件，或者在最终提交之后不再重试）。\n"
               "        后果: 实盘重复下单（代引订单），可能被店铺拉黑。" + tail(bot))


@case("E06", "最终确认", "[实盘] 确认页同时含『修正する』(type=submit) 与『注文する』按钮：不能把修正按钮一并提交导致退回表单", "高")
async def e06():
    srv = FakeFC2Server()
    srv.add_item("636", stock=3)
    def _confirm_with_submit_back(ctx: ReqCtx) -> str:
        return wrap_min(
            '<div class="contents" id="checkout_page"><h2>ご注文内容の確認</h2>'
            f'{TPL_REGI_FORM}<input type="hidden" name="confirm_token" value="{ctx.session.confirm_token}" />'
            '<p class="registerbutton">'
            '<input type="submit" name="back" value="修正する" class="btn_submit_return" />'
            '<input type="submit" name="Submit2" value="注文する" class="btn_submit_order" />'
            '</p></form></div>'
        )
    with fc2_network(srv):
        chk = new_checkout(dry_run=False)
        try:
            await chk.add_to_cart("636")
            _, fctx, _ = await chk.get_regi_form()
            await chk.submit_regi_info(fctx)
            sess = list(srv.sessions.values())[0]
            confirm_html = _confirm_with_submit_back(ReqCtx("POST", "/regi", {}, sess, 0.0))
            res = await chk.confirm_final_order(confirm_html)
        finally:
            await chk.close()
    last_post = [r for r in srv.requests if r.method == "POST" and r.path == "/regi"][-1]
    expect(len(srv.orders) == 1 and "back" not in last_post.form,
           f"最终提交把确认表单里所有 <input> 原样打包发送: {dict(last_post.form)}，其中包含 back='修正する'，"
           f"导致服务器按『返回修改』处理，实际生成订单数={len(srv.orders)} (返回 msg={res.get('message')})。\n"
           "        原因: confirm_final_order 遍历 form.find_all('input') 时未过滤非确认用途的 submit/button/reset 或未勾选的 radio/checkbox。\n"
           "        后果: 只要确认页的『戻る/修正する』也是 input[type=submit]，最终提交就会退回上一页而无法成单。")


@case("E07", "最终确认", "[实盘] 最终提交瞬间最后 1 件被抢光 (カートにある商品が売り切れました)：应标记 is_soldout=True", "中")
async def e07():
    srv = FakeFC2Server()
    srv.add_item("637", stock=1)
    srv.inject("POST", "/regi", "hook", lambda c: setattr(srv.items["637"], "stock", 0), when=lambda c: "Submit2" in c.form)
    with fc2_network(srv):
        res = await new_checkout(dry_run=False).execute_checkout("637", "テスト")
    expect(not res.get("success") and res.get("is_soldout") is True,
           f"最终确认提交时返回『カートにある商品が売り切れました』，但 execute_checkout 返回 is_soldout={res.get('is_soldout')}, msg={res.get('message')}。\n"
           "        原因: execute_checkout 在第 4 步结尾硬编码了 'is_soldout': False。\n"
           "        后果: 调用方拿到 is_soldout=False，不能直接通过该字段获知商品已在最后一步售罄。")


# =========================================================================
# F. 机器人并发与调度机制
# =========================================================================
@case("F01", "并发机制", "worker 遇连续繁忙 (503×4) 后最终模拟下单成功", "基线")
async def f01():
    srv = FakeFC2Server()
    srv.add_item("701", stock=3)
    srv.inject("POST", "/cart_in", "status", 503, times=4)
    async with sim_bot(srv, order={"worker_retries": 5}) as bot:
        await run_worker(bot, make_item("701"))
        expect("701" in bot.simulated_item_ids, "worker 未能在繁忙后成功" + tail(bot))


@case("F02", "并发机制", "worker 遇真售罄：立即止损、标记售罄、只请求 1 次", "基线")
async def f02():
    srv = FakeFC2Server()
    srv.add_item("702", stock=0)
    async with sim_bot(srv) as bot:
        await run_worker(bot, make_item("702"))
        n = srv.count("POST", "/cart_in")
        expect("702" in bot.soldout_item_ids and n == 1, f"soldout={bot.soldout_item_ids}, cart_in 次数={n}" + tail(bot))


@case("F03", "并发机制", "取消 worker 后立即重新派发：旧任务清理不能误删新任务 (防同款并发重复下单)", "高")
async def f03():
    srv = FakeFC2Server()
    srv.add_item("703", stock=3)
    srv.latency = 0.3
    async with sim_bot(srv) as bot:
        item = make_item("703")
        bot.dispatch_order_worker(item)
        t1 = bot.active_order_tasks["703"]
        bot._sim_tasks.append(t1)
        await asyncio.sleep(0.05)
        bot.cancel_order_worker("703", reason="测试取消")
        bot.dispatch_order_worker(item)
        t2 = bot.active_order_tasks.get("703")
        bot._sim_tasks.append(t2)
        await asyncio.sleep(0.1)  # 让 t1 处理完取消
        cur = bot.active_order_tasks.get("703")
        detail = ""
        if cur is not t2:
            bot.dispatch_order_worker(item)
            t3 = bot.active_order_tasks.get("703")
            if t3:
                bot._sim_tasks.append(t3)
            running = sum(1 for t in {t2, t3} if t and not t.done())
            detail = f"随后再派发一次 → 同一商品同时有 {running} 个下单线程在跑。"
        expect(cur is t2,
               "旧 worker 被取消后，它的 finally 和 done_callback 按 item_id 执行了 pop，把刚派发的新 worker 也从 "
               f"active_order_tasks 里删掉了 (当前登记={cur})。{detail}\n"
               "        原因: _run_order_worker 的 finally 和 add_done_callback(_cleanup_order_task) 都只按 item_id 删除，"
               "没有核对删的是不是自己这个 task。\n"
               "        后果: 去重机制失效，同一款商品可能出现多个并发下单线程 → 实盘重复下单。")


@case("F04", "并发机制", "维护结束直发：4 款重点商品 (并发上限3) 每款都应立刻发出加购", "高")
async def f04():
    srv = FakeFC2Server()
    ids = ["711", "712", "713", "714"]
    for i in ids:
        srv.add_item(i, stock=5)
    srv.inject("POST", "/cart_in", "status", 503, times=-1)
    async with sim_bot(srv, order={"session_retries": 1, "max_concurrent_workers": 3}) as bot:
        for i in ids:
            bot.upcoming_items[i] = {"item_id": i, "title": f"ドール{i}", "url": f"{BASE}/ca13/{i}/"}
        bot.trigger_maintenance_direct_orders()
        await asyncio.sleep(1.5)
        counts = {i: srv.count("POST", "/cart_in", item_id=i) for i in ids}
        starving = [i for i, c in counts.items() if c == 0]
        expect(not starving,
               f"开售后 1.5s 内各商品的加购请求数: {counts}，商品 {starving} 一次都没发出去。\n"
               "        原因: worker 在整个重试循环 (维护直发默认 60 轮) 期间一直持有 Semaphore(max_concurrent_workers=3)，"
               "第 4 款要等前面某一款 60 轮全部打完才能拿到名额。\n"
               "        后果: 重点商品超过 3 款时，后面的商品在开售最关键的几十秒里完全没有出手。")


@case("F05", "并发机制", "服务器慢但可用 (每请求1.3s)：对冲机制不能把唯一能成功的主请求杀掉", "高", timeout=30)
async def f05():
    srv = FakeFC2Server()
    srv.add_item("721", stock=3)
    srv.latency = 1.3
    async with sim_bot(srv, order={"hedge_requests": True, "hedge_delay": 0.3, "worker_retries": 1, "session_retries": 1}) as bot:
        t0 = time.perf_counter()
        await run_worker(bot, make_item("721"), timeout=20)
        el = time.perf_counter() - t0
        n_sessions = len({r.session.sid for r in srv.requests if r.path == "/cart_in"})
        expect("721" in bot.simulated_item_ids,
               f"服务器每个请求 1.3s 都能正常响应，跑完一整单约 5.2s，但 worker 在 {el:.1f}s 时放弃了；"
               f"期间开了 {n_sessions} 个加购会话。\n"
               "        原因: 主请求最多只能活 hedge_delay + 4×hedge_delay + 3s (默认 2.5+10+3 = 15.5s)；"
               "对冲探针每个只给 hedge_delay 秒，而一整单要 4 个串行请求，探针永远跑不完。\n"
               "        后果: 抢购高峰每个请求要 4s 左右时，整单需要 16s 以上 → 主请求每一轮都在快成功时被取消，永远下不了单，"
               "而且探针不断新开会话加购，进一步加重服务器负载。" + tail(bot))


@case("F06", "并发机制", "[实盘] 主请求最终提交已出单但响应慢，对冲探针不能再下第二单", "致命")
async def f06():
    srv = FakeFC2Server()
    srv.add_item("722", stock=5)
    srv.inject("POST", "/regi", "process_then_delay", 1.0, when=lambda c: "Submit2" in c.form)
    async with sim_bot(srv, order={"dry_run": False, "hedge_requests": True, "hedge_delay": 0.3,
                                   "worker_retries": 2, "session_retries": 1}) as bot:
        await run_worker(bot, make_item("722"), timeout=10)
        expect(len(srv.orders) == 1,
               f"服务器上生成了 {len(srv.orders)} 个订单 (分别来自会话 {[o['sid'] for o in srv.orders]})\n"
               "        原因: 对冲探针是一套完整的独立结算流程（自己的购物车、自己的最终提交）；主请求的最终提交已被服务器受理，"
               "只是响应慢，探针仍会再完整下一单，然后才 cancel 主请求（服务器上的订单已经撤不回来）。\n"
               "        后果: 实盘重复下单。" + tail(bot))


@case("F07", "并发机制", "[实盘] 抢到最后 1 件后详情页变售罄：机器人必须记录为已购，不能取消成『售罄』", "高")
async def f07():
    srv = FakeFC2Server()
    srv.add_item("723", stock=1)
    srv.inject("POST", "/regi", "process_then_delay", 1.0, when=lambda c: "Submit2" in c.form)
    async with sim_bot(srv, order={"dry_run": False, "session_retries": 1}) as bot:
        start_priority_monitor(bot, "723")
        await wait_until(lambda: bool(srv.orders), 3.0)
        await asyncio.sleep(1.8)
        expect("723" in bot.purchased_item_ids,
               f"服务器订单={[o['order_id'] for o in srv.orders]}，但 purchased_item_ids={bot.purchased_item_ids}，"
               f"soldout_item_ids={bot.soldout_item_ids}\n"
               "        原因: 我们的最终提交拿走了最后一件 → 详情页变成『在庫切れ』→ 专属监控调用 cancel_order_worker，"
               "把还在等最终提交响应的 worker 杀掉了。\n"
               "        后果: 实际已下单，机器人却报告『售罄』；max_orders 计数没有增加，会接着去抢其他款，可能超出预算。"
               + tail(bot, 8))


@case("F08", "并发机制", "详情页偶发残缺 (无库存栏/无购物车按钮)：不能永久判为售罄", "高")
async def f08():
    srv = FakeFC2Server()
    it = srv.add_item("724", stock=3)
    partial = srv.product_page(it, drop_stock_li=True, cart_button=False)
    srv.inject("GET", "/ca13/724/", "html", partial, times=2)
    async with sim_bot(srv) as bot:
        start_priority_monitor(bot, "724")
        await wait_until(lambda: "724" in bot.simulated_item_ids or "724" in bot.soldout_item_ids, 3.0)
        await asyncio.sleep(0.3)
        expect("724" not in bot.soldout_item_ids and "724" in bot.simulated_item_ids,
               f"详情页只是偶发返回了一次不完整的页面，商品就被永久判为售罄 (soldout={bot.soldout_item_ids})，"
               f"后面页面恢复正常也没有再抢。\n"
               "        原因: bot.py 里 is_soldout_detail = any(售罄词) or not target_item.in_stock —— 后半句让『既不是预告也不是在库』"
               "的任何状态都算售罄，并且立即 break 退出专属监控。\n"
               "        后果: 一次页面抖动就会永久放弃这款重点商品。" + tail(bot))


@case("F09", "并发机制", "预告文案不含『予定』(如『１１月８日２１時発売』)：不能误判在售/售罄", "中")
async def f09():
    problems = []
    mon = FC2Monitor(BASE)
    srv0 = FakeFC2Server()
    it0 = srv0.add_item("725", upcoming=True, stock_text="１１月８日２１時発売", cart_button=True)
    parsed = mon.parse_single_item_page(srv0.product_page(it0), f"{BASE}/ca13/725/")
    if not parsed.is_upcoming:
        problems.append(f"[真实模板·有购物车按钮] 解析结果 is_upcoming={parsed.is_upcoming}, in_stock={parsed.in_stock} "
                        "→ 未开售就被当成在售，开售前会持续派发下单线程空打 cart_in")

    srv = FakeFC2Server()
    srv.add_item("726", upcoming=True, stock_text="１１月８日２１時発売", cart_button=False)
    async with sim_bot(srv) as bot:
        start_priority_monitor(bot, "726")
        await asyncio.sleep(0.6)
        if "726" in bot.soldout_item_ids:
            problems.append("[无购物车按钮] 预告商品被永久判为售罄，专属监控退出，维护结束直发也会跳过它")
    expect(not problems, "\n        ".join(problems) +
           "\n        原因: UPCOMING_KEYWORDS 只认『予定/開始予定/発売予定/販売予定/販売開始前/coming soon』；"
           "店铺只要换个写法（『発売』『より販売』『近日』等）就会被判错。")


@case("F10", "并发机制", "维护 → 前哨识别 → 开门 → 直发加购穿越 503×3 → 模拟下单成功 (端到端)", "基线")
async def f10():
    srv = FakeFC2Server()
    it = srv.add_item("727", stock=3, upcoming=True)
    srv.maintenance = True
    async with sim_bot(srv) as bot:
        start_priority_monitor(bot, "727")
        in_maint = await wait_until(lambda: bot.is_in_maintenance, 2.0)
        expect(in_maint, "维护状态未被前哨探测识别" + tail(bot))
        srv.inject("POST", "/cart_in", "status", 503, times=3)
        it.upcoming = False
        srv.maintenance = False
        ok = await wait_until(lambda: "727" in bot.simulated_item_ids, 5.0)
        expect(ok and not bot.is_in_maintenance, f"开门后未能完成直发下单: simulated={bot.simulated_item_ids}" + tail(bot))


@case("F11", "并发机制", "加购遇 429 限流×2：worker 跨轮次最终成功", "基线")
async def f11():
    srv = FakeFC2Server()
    srv.add_item("728", stock=3)
    srv.inject("POST", "/cart_in", "status", 429, times=2)
    async with sim_bot(srv, order={"worker_retries": 5}) as bot:
        await run_worker(bot, make_item("728"))
        expect("728" in bot.simulated_item_ids, "429 后未能恢复" + tail(bot))


@case("F12", "并发机制", "[实盘] max_orders=1 时 2 款重点商品同时开售：严禁并发竞态下出 2 单 (超买风险)", "致命")
async def f12():
    srv = FakeFC2Server()
    srv.add_item("731", stock=3)
    srv.add_item("732", stock=3)
    # 模拟真实服务器处理完订单后响应在网络上传输 0.15s 才回到客户端
    srv.inject("POST", "/regi", "process_then_delay", 0.15, times=2, when=lambda c: "Submit2" in c.form)
    async with sim_bot(srv, order={"dry_run": False, "max_orders": 1, "max_concurrent_workers": 3}) as bot:
        bot.upcoming_items["731"] = {"item_id": "731", "title": "ドール731", "url": f"{BASE}/ca13/731/"}
        bot.upcoming_items["732"] = {"item_id": "732", "title": "ドール732", "url": f"{BASE}/ca13/732/"}
        bot.trigger_maintenance_direct_orders()
        tasks = [t for t in bot.active_order_tasks.values() if t]
        bot._sim_tasks.extend(tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
        expect(len(srv.orders) == 1 and len(bot.purchased_item_ids) == 1,
               f"用户设定 max_orders=1 (最多只买 1 款)，但服务器实际生成了 {len(srv.orders)} 个真实订单: "
               f"{[(o['order_id'], o['items']) for o in srv.orders]}。\n"
               "        原因: dispatch_order_worker 和 _run_order_worker 只在下单完成时才写入 purchased_item_ids，"
               "没有任何『在途名额预占』或『最终提交互斥锁』；2 个并发线程启动时都看到 len(purchased_item_ids)==0 < 1，在真实网络延迟下同时发出 Submit2 完成结账。\n"
               "        后果: 用户监控多款候选娃、只想买最先抢到的 1 款时，实盘会同时买下多款，造成超额支付。" + tail(bot))


@case("F13", "并发机制", "全量货架同步 (sync_full_catalog) 遇单页/分页 503：不能把已锁定的预计上线商品抹除", "高")
async def f13():
    srv = FakeFC2Server()
    # 构造 21 个商品（分 2 页：page 0 有 20 个在售商品，page 1 有 1 个预计上线重点商品 799）
    for idx in range(1, 21):
        srv.add_item(f"{800 + idx}", stock=1)
    srv.add_item("799", upcoming=True)
    async with sim_bot(srv) as bot:
        bot.upcoming_items["799"] = {"item_id": "799", "title": "ドール799", "url": f"{BASE}/ca13/799/", "is_upcoming": True}
        # 模拟全量同步时某页或整体只返回了前 20 个在售商品，未含 799
        orig_scan = bot.monitor.scan_full_catalog
        async def _partial_scan(max_pages: int = 25):
            all_it = await orig_scan(max_pages=max_pages)
            return [it for it in all_it if it.item_id != "799"]
        bot.monitor.scan_full_catalog = _partial_scan
        await bot.sync_full_catalog()
        expect("799" in bot.upcoming_items,
               f"全量同步漏掉某页后，原先已锁定的预计上线商品 799 从 upcoming_items 中被彻底抹除 (当前 upcoming_items={list(bot.upcoming_items.keys())})。\n"
               "        原因: sync_full_catalog 直接执行 self.upcoming_items = upcoming_dict 全量覆盖，而不是合并保留尚未售罄/未购买的已知预告商品。\n"
               "        后果: 定时同步或手动刷新时只要某一页因网络抖动失败，该页上的待抢新品就会丢失专属监控与预热连接。")


@case("F14", "并发机制", "单次请求正常快速返回但多次内循环累计超 hedge_delay 时：不误发对冲探针，且日志准确显示重试次数与累计耗时", "高")
async def f14():
    srv = FakeFC2Server()
    srv.add_item("729", stock=0, upcoming=True)  # 尚未开售 -> 每次 POST /cart_in 返回空购物车
    srv.latency = 0.04  # 每次请求仅 40ms (远小于 hedge_delay=0.20s)，但连跑 8 次累计约 0.40s > hedge_delay
    async with sim_bot(srv, order={
        "hedge_requests": True,
        "hedge_delay": 0.20,
        "session_retries": 8,
        "session_retry_delay": 0.01,
        "worker_retries": 1
    }) as bot:
        res = await bot.manual_test_checkout("729", "未开售测试商品")
        msg = res.get("message", "")
        expect("重试8次" in msg and "累计" in msg and "末次" in msg,
               f"多次重试后日志未展示重试次数与累计耗时: {msg}")

        srv.requests.clear()
        await run_worker(bot, make_item("729"), timeout=5)
        hedge_logs = [m for m in bot.logs if "启动滚动对冲探针" in m]
        expect(not hedge_logs and srv.count("POST", "/cart_in") == 8,
               f"每次请求仅 40ms (< hedge_delay 200ms)，却误触发了对冲探针！日志={hedge_logs}, 总请求数={srv.count('POST', '/cart_in')}")


@case("F15", "并发机制", "[实盘] 前4次快回空车(不误发探针) → 第5次开售瞬间主连接卡队列 → 探针精准触发反超出单", "高")
async def f15():
    srv = FakeFC2Server(real_templates=True)
    it = srv.add_item("735", stock=2, upcoming=True)
    cart_calls = 0
    orig_dispatch = srv._dispatch

    async def custom_dispatch(request, ctx):
        nonlocal cart_calls
        if ctx.method == "POST" and ctx.path == "/cart_in":
            cart_calls += 1
            if cart_calls <= 4:
                # 前 4 次尚未开放库存，30ms 快速返回空购物车 (远小于 hedge_delay=0.22s)
                await asyncio.sleep(0.03)
            elif cart_calls == 5:
                # 第 5 次起正式开放库存！但第 5 次（主连接）遭遇服务器慢队列卡死 1.5s
                it.upcoming = False
                await asyncio.sleep(1.5)
            else:
                # 第 6 次（对冲探针#1）走新通道 20ms 极速响应！
                it.upcoming = False
                await asyncio.sleep(0.02)
        return await orig_dispatch(request, ctx)

    srv._dispatch = custom_dispatch
    async with sim_bot(srv, order={
        "dry_run": False,
        "hedge_requests": True,
        "hedge_delay": 0.22,
        "session_retries": 100,
        "session_retry_delay": 0.01,
        "worker_retries": 2
    }) as bot:
        t0 = time.perf_counter()
        await run_worker(bot, make_item("735"), timeout=6)
        elapsed = time.perf_counter() - t0
        expect("735" in bot.purchased_item_ids and len(srv.orders) == 1 and elapsed < 1.2,
               f"purchased={bot.purchased_item_ids}, orders={len(srv.orders)}, elapsed={elapsed:.2f}s" + tail(bot, 10))


@case("F16", "并发机制", "[实盘·全真高压风暴] 4款重点新品(343/344/352/353)维护结束齐发：混合503/繁忙页/排队卡死/部分售罄", "致命", timeout=15)
async def f16():
    srv = FakeFC2Server(real_templates=True)
    it343 = srv.add_item("343", title="ちっちゃなシリーズ ちぃち", stock=2, upcoming=True)
    it344 = srv.add_item("344", title="ちっちゃなシリーズ なぁな", stock=2, upcoming=True)
    it352 = srv.add_item("352", title="ちっちゃなシリーズ ひな", stock=2, upcoming=True)
    it353 = srv.add_item("353", title="ちっちゃなシリーズ ひよ", stock=0, upcoming=True)  # 开门即秒光
    srv.maintenance = True

    # 343: 加购前 3 次 503 + GET /regi 1 次 200 繁忙页
    srv.inject("POST", "/cart_in", "status", 503, times=3, when=lambda c: c.form.get("item_id") == "343")
    srv.inject("GET", "/regi", "busy200", times=1)
    # 344: 主连接第 1 次加购排队卡顿 1.2s (触发对冲探针救场) + POST /regi(Submit1) 1 次 502
    srv.inject("POST", "/cart_in", "delay", 1.2, times=1, when=lambda c: c.form.get("item_id") == "344")
    srv.inject("POST", "/regi", "status", 502, times=1, when=lambda c: "Submit1" in c.form)
    # 352: 最终确认 Submit2 遇 1 次 503 后同会话恢复
    srv.inject("POST", "/regi", "status", 503, times=1, when=lambda c: "Submit2" in c.form)

    async with sim_bot(srv, order={
        "dry_run": False,
        "max_orders": 0,
        "max_concurrent_workers": 8,
        "session_retries": 100,
        "session_retry_delay": 0.01,
        "worker_retries": 100,
        "worker_retry_delay": 0.02,
        "hedge_requests": True,
        "hedge_delay": 0.25,
        "maintenance_direct_cart": True
    }) as bot:
        for iid in ("343", "344", "352", "353"):
            bot.upcoming_items[iid] = {"item_id": iid, "title": f"ドール{iid}", "url": f"{BASE}/ca13/{iid}/", "is_upcoming": True}
            start_priority_monitor(bot, iid)

        in_maint = await wait_until(lambda: bot.is_in_maintenance, 2.0)
        expect(in_maint, "未能进入维护状态" + tail(bot))

        # 维护结束，343/344/352 开放库存，353 秒光
        for it in (it343, it344, it352, it353):
            it.upcoming = False
        srv.maintenance = False

        t0 = time.perf_counter()
        done_all = await wait_until(
            lambda: {"343", "344", "352"}.issubset(bot.purchased_item_ids) and "353" in bot.soldout_item_ids,
            6.0
        )
        elapsed = time.perf_counter() - t0
        ordered_items = sorted(list(o["items"].keys())[0] for o in srv.orders)
        expect(
            done_all and ordered_items == ["343", "344", "352"] and "353" in bot.soldout_item_ids,
            f"4款高压齐发结果异常 (耗时 {elapsed:.2f}s): purchased={bot.purchased_item_ids}, "
            f"soldout={bot.soldout_item_ids}, 实际出单={ordered_items}" + tail(bot, 12)
        )


@case("F17", "并发机制", "跨轮次重试时复用温热 TLS 连接池 (不销毁 AsyncClient) 且自动清空旧轮次 Cookie", "高")
async def f17():
    srv = FakeFC2Server()
    srv.add_item("737", stock=2)
    # 第 1 轮在 GET /regi 时连续 503 耗尽单轮 session_retries=2，迫使进入第 2 轮 worker attempt
    srv.inject("GET", "/regi", "status", 503, times=2)
    seen_clients = []
    orig_exec = FC2Checkout.execute_checkout

    async def tracked_exec(self, *args, **kwargs):
        seen_clients.append(self.client)
        return await orig_exec(self, *args, **kwargs)

    FC2Checkout.execute_checkout = tracked_exec
    try:
        async with sim_bot(srv, order={
            "dry_run": False,
            "hedge_requests": False,
            "session_retries": 2,
            "session_retry_delay": 0.01,
            "worker_retries": 3,
            "worker_retry_delay": 0.01
        }) as bot:
            await run_worker(bot, make_item("737"), timeout=5)
            expect(
                "737" in bot.purchased_item_ids and len(seen_clients) == 2 and seen_clients[0] is seen_clients[1],
                f"跨轮次未复用同一个底层 AsyncClient 连接池: seen_clients={seen_clients}, purchased={bot.purchased_item_ids}"
            )
    finally:
        FC2Checkout.execute_checkout = orig_exec




# =========================================================================
# Runner
# =========================================================================
async def run_case(c: Case) -> Tuple[str, str, float]:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(c.fn(), timeout=c.timeout)
        return "PASS", "", time.perf_counter() - t0
    except BugFound as e:
        return "BUG", str(e), time.perf_counter() - t0
    except asyncio.TimeoutError:
        return "ERROR", f"用例超时 (>{c.timeout}s)", time.perf_counter() - t0
    except Exception:
        return "ERROR", traceback.format_exc(), time.perf_counter() - t0


async def main(selected: List[str]) -> int:
    cases = [c for c in CASES if not selected or c.cid in selected]
    print(f"\n{BOLD}{GREEN}{'#' * 78}{RESET}")
    print(f"{BOLD}{GREEN}  🧸 FC2 有状态模拟服务器 · 全步骤故障注入测试  ({len(cases)} 个用例){RESET}")
    print(f"{BOLD}{GREEN}{'#' * 78}{RESET}")
    results = []
    group = None
    t_all = time.perf_counter()
    for c in cases:
        if c.group != group:
            group = c.group
            print(f"\n{BOLD}{CYAN}━━ {group} ━━{RESET}")
        status, detail, el = await run_case(c)
        results.append((c, status, detail))
        if status == "PASS":
            print(f"  {GREEN}✅ {c.cid}{RESET} {c.title} {DIM}({el:.1f}s){RESET}")
        elif status == "BUG":
            print(f"  {RED}🐞 {c.cid} [{c.severity}]{RESET} {c.title} {DIM}({el:.1f}s){RESET}")
            print(f"     {YELLOW}↳ {detail}{RESET}")
        else:
            print(f"  {YELLOW}⚠️  {c.cid} 测试自身异常{RESET} {c.title}\n{detail}")

    bugs = [(c, d) for c, s, d in results if s == "BUG"]
    errs = [c for c, s, _ in results if s == "ERROR"]
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n{BOLD}{CYAN}{'=' * 78}{RESET}")
    print(f"{BOLD}结果: {GREEN}{passed} 通过{RESET}{BOLD} | {RED}{len(bugs)} 发现缺陷{RESET}{BOLD} | "
          f"{YELLOW}{len(errs)} 测试异常{RESET}{BOLD} | 耗时 {time.perf_counter() - t_all:.1f}s{RESET}")
    if bugs:
        order = {"致命": 0, "高": 1}
        print(f"\n{BOLD}缺陷清单 (按严重度):{RESET}")
        for c, _ in sorted(bugs, key=lambda x: order.get(x[0].severity[:2].strip("("), 2)):
            print(f"  {RED}[{c.severity}]{RESET} {c.cid} {c.title}")
    print(f"{BOLD}{CYAN}{'=' * 78}{RESET}\n")
    return 1 if (bugs or errs) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main([a.upper() for a in sys.argv[1:]])))
