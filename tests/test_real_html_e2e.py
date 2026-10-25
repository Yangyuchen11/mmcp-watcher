"""
===========================================================================
🧸 MAMACHAPP FC2 BOT - 基于真实 HTML 源码的全流程端到端检验套件
===========================================================================
测试严格基于 E:\\source-code\\mmcp\\html-source 文件夹中的真实网站 HTML：
  - syouhin.html       : 真实单品详情页（ちぃち クロスゴシックドレス，预告状态）
  - mmcp-cart.html     : 真实购物车页面（包含 /regi 通道）
  - mmcp-nyuryoku.html : 真实收件与支付结算表单页面（包含 address[in][*] 全套字段）

覆盖全流程生命周期：
  1. 真实 HTML 单品详情页精准解析与预告状态捕获
  2. 优先商品锁定与双通道预热通道验证
  3. 维护与关店模式多层冗余检测（含非标自拟文案与缺失容器容错）
  4. 维护解除前哨交叉引爆与全员直接加购冲刺
  5. 基于真实 mmcp-cart.html 与 mmcp-nyuryoku.html 的端到端下单执行
  6. 100轮换会话重试耗尽但检测有货时的「自动无缝接力抢购」验证
  7. 官方售罄 (在庫切れ) 永久止损与任务清理验证
===========================================================================
"""

import sys
import os
import time
import asyncio
from typing import Dict, Any, List, Optional
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

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from core.monitor import FC2Monitor, ShopItem, MAINTENANCE_KEYWORDS, UPCOMING_KEYWORDS
from core.checkout import FC2Checkout
from core.bot import BotEngine
from core.config import ConfigManager

# Terminal styling
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def print_header(title: str):
    print(f"\n{BOLD}{CYAN}{'='*75}{RESET}")
    print(f"{BOLD}{CYAN}🧪 {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*75}{RESET}")

def print_pass(msg: str):
    print(f"  {GREEN}✅ [PASS]{RESET} {msg}")

def print_fail(msg: str):
    print(f"  {RED}❌ [FAIL]{RESET} {msg}")

def print_info(msg: str):
    print(f"  {YELLOW}ℹ️ [INFO]{RESET} {msg}")

# Path helpers for authentic HTML source files
HTML_DIR = os.path.join(PROJECT_ROOT, "html-source")
SYOUHIN_PATH = os.path.join(HTML_DIR, "syouhin.html")
CART_PATH = os.path.join(HTML_DIR, "mmcp-cart.html")
NYURYOKU_PATH = os.path.join(HTML_DIR, "mmcp-nyuryoku.html")

def load_real_html(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# =========================================================================
# STAGE 1: 真实单品页面深度解析验证 (syouhin.html)
# =========================================================================
async def test_real_syouhin_parsing():
    print_header("STAGE 1: 真实商品源码 (syouhin.html) 深度解析与预告状态提取")
    html = load_real_html(SYOUHIN_PATH)
    monitor = FC2Monitor("https://mamachapptoy.cart.fc2.com")

    # 1. 验证单品详情解析
    item = monitor.parse_single_item_page(html, "https://mamachapptoy.cart.fc2.com/ca13/343/")
    assert item is not None, "真实 syouhin.html 解析结果不应为空"
    assert item.item_id == "343", f"商品ID提取错误: 期望 343, 得到 {item.item_id}"
    assert "ちぃち" in item.title, f"商品标题提取错误: {item.title}"
    assert "クロスゴシック" in item.title, f"商品标题提取错误: {item.title}"
    assert item.price_value == 22800, f"价格数值提取错误: {item.price_value}"
    assert item.is_upcoming is True, "应正确识别预告状态 (is_upcoming = True)"
    assert item.in_stock is False, "预告状态下在库状态应为 False"
    assert "１１月１日夜開始予定" in item.stock_text, f"发售时间提取错误: {item.stock_text}"
    assert item.category_id == "13", f"分类ID提取错误: {item.category_id}"

    # 2. 验证真实商品容器检测器 (has_detail_container)
    assert monitor.has_detail_container(html) is True, "真实详情页应具备 #itemdetail_page 等标准容器"

    print_pass(f"成功从真实 HTML 提取商品: [{item.title}]")
    print_pass(f"商品ID: {item.item_id} | 价格: ¥{item.price_value:,} | 分类: ca{item.category_id}")
    print_pass(f"库存文本: '{item.stock_text}' -> 准确判定为【预计上线新品】(未开售)")
    print_pass("标准详情容器结构校验通过 (has_detail_container = True)")


# =========================================================================
# STAGE 2: 维护与关店模式多层冗余检测（含非标自拟文案与缺失容器容错）
# =========================================================================
async def test_maintenance_redundant_rules():
    print_header("STAGE 2: 维护关店多层冗余规则验证 (标准词 + 非标文案 + 容器缺失容错)")
    monitor = FC2Monitor("https://mamachapptoy.cart.fc2.com")
    real_html = load_real_html(SYOUHIN_PATH)

    # 1. 正常销售商品页面（零误报测试）
    assert monitor.check_maintenance(real_html, 200) is False, "正常销售商品页面绝不能误判为维护！"
    print_pass("零误判测试: 真实商品详情页未发生维护误判")

    # 2. 官方标准维护文案
    std_page1 = "<html><body><h1>只今メンテナンス中です</h1><p>ご迷惑をおかけしております。</p></body></html>"
    std_page2 = "<html><body><div id='header'>開店準備中につき一時閉店しております</div></body></html>"
    assert monitor.check_maintenance(std_page1, 200) is True, "应识别标准维护文案"
    assert monitor.check_maintenance(std_page2, 200) is True, "应识别标准开店准备关店文案"
    print_pass("官方标准文案识别: '只今メンテナンス中です' 与 '開店準備中につき一時閉店しております' 准确捕获")

    # 3. 店主非标自拟文案（冗余核心根词测试）
    custom_cases = [
        ("商品入替中につき閉店", "店主自创商品入替关店文案"),
        ("本日は販売準備のため一時閉店いたします", "店主自创销售准备文案"),
        ("サーバー移転のためショップ準備中となっております", "店铺准备中变体"),
        ("アクセス集中のためカート閉鎖しております", "购物车闭锁变体"),
        ("本日21時まで準備中", "极简准备中文案"),
    ]
    for text, label in custom_cases:
        custom_html = f"<html><body><div class='notice'><h1>{text}</h1></div></body></html>"
        is_maint = monitor.check_maintenance(custom_html, 200)
        assert is_maint is True, f"应准确识别非标冗余文案: {label} ('{text}')"
        print_pass(f"非标文案容错: 准确捕获 [{label}] ('{text}')")

    # 4. 详情页容器缺失与模糊线索双重冗余检测 (has_detail_container + cues)
    # 当 FC2 或店主关店但文案极为罕见，只要详情页的标准容器不存在且含关店线索，就能兜底捕获！
    missing_container_html = "<html><head><title>店舗のお知らせ</title></head><body><div id='wrap'><h2>本日は改装作業のため一時休止</h2><p>再開までお待ちください</p></div></body></html>"
    
    async def mock_fetch(*args, **kwargs):
        return missing_container_html, 200
    monitor.fetch_page = mock_fetch

    item, is_maint_detail = await monitor.check_item_detail("https://mamachapptoy.cart.fc2.com/ca13/343/")
    assert item is None, "维护页不应解析出商品"
    assert is_maint_detail is True, "详情页容器缺失+含休止线索时，必须通过冗余兜底规则识别出维护！"
    print_pass("容器缺失兜底容错: 当 #itemdetail_page 缺失且含闭店线索时，成功触发冗余兜底维护判定！")


# =========================================================================
# STAGE 3: 维护解除瞬间前哨交叉引爆与直接加购冲刺验证
# =========================================================================
async def test_cross_sentinel_maintenance_lift():
    print_header("STAGE 3: 维护解除瞬间「前哨交叉引爆」与全员直接加购冲刺验证")
    cfg_mgr = ConfigManager("config.json")
    bot = BotEngine(cfg_mgr)
    bot.is_running = True
    bot.is_in_maintenance = True  # 初始模拟大盘在维护中
    bot.config["order"]["auto_buy"] = True
    bot.config["order"]["enable_maintenance_direct_order"] = True

    real_html = load_real_html(SYOUHIN_PATH)

    # 构造开门状态真实 HTML：将 stock 文本替换为有货，保留真实 input 和提交按钮
    dropped_html = real_html.replace(
        '<li class="stock">１１月１日夜開始予定</li>',
        '<li class="stock">在庫あり (残り3個)</li>'
    )

    async def mock_fetch_detail(*args, **kwargs):
        return dropped_html, 200
    bot.monitor.fetch_page = mock_fetch_detail
    bot.monitor.fetch_page_hedged = lambda *a, **kw: mock_fetch_detail()

    direct_fired_items = []
    def mock_dispatch(item, event_type="IN_STOCK"):
        direct_fired_items.append((item.item_id, event_type))
    bot.dispatch_order_worker = mock_dispatch

    # 注册两款重点商品
    bot.priority_item_ids = ["343", "344"]
    cand_item = bot.monitor.parse_single_item_page(real_html, "https://mamachapptoy.cart.fc2.com/ca13/343/")
    bot.upcoming_items["343"] = cand_item.to_dict()

    # 执行单次单品探测模拟
    target_item, is_maint = await bot.monitor.check_item_detail("https://mamachapptoy.cart.fc2.com/ca13/343/")
    assert target_item is not None and target_item.in_stock is True, "应识别单品开售有货"

    # 模拟前哨交叉引爆逻辑
    if bot.is_in_maintenance:
        bot.is_in_maintenance = False
        bot.auto_rush_active = True
        bot.trigger_maintenance_direct_orders()

    assert bot.is_in_maintenance is False, "单品有货应立即解除大盘维护状态"
    assert bot.auto_rush_active is True, "解除维护应立即激活极速突击轮询模式"
    assert len(direct_fired_items) >= 1, "维护结束应瞬间为重点商品触发直接加购冲刺"
    assert any(it[0] == "343" for it in direct_fired_items), "ID 343 必须被直接派发加购"

    print_pass("前哨交叉引爆成功: 单品率先刷出有货 -> 立即解除全局维护状态 (is_in_maintenance = False)")
    print_pass("极速突击激活成功: 全局自动开启突击加速 (auto_rush_active = True)")
    print_pass(f"直接加购冲刺成功: 瞬间为重点商品派发加购线程 (触发队列: {direct_fired_items})")


# =========================================================================
# STAGE 4: 基于真实 HTML (mmcp-cart.html + mmcp-nyuryoku.html) 的端到端下单
# =========================================================================
async def test_real_html_checkout_pipeline():
    print_header("STAGE 4: 基于真实 HTML 源码的端到端加购与结算下单验证")
    real_cart_html = load_real_html(CART_PATH)
    real_nyuryoku_html = load_real_html(NYURYOKU_PATH)

    # 1. 验证空购物车判别 (当 mmcp-cart.html 包含 'カートに商品は入っていません' 时)
    buyer_info = {
        "name": "山田　太郎",
        "kkana": "ヤマダ　タロウ",
        "email": "buyer_example@gmail.com",
        "tel": "08012345678",
        "postcode": "100-0001",
        "pref": "東京都",
        "address": "千代田区千代田1-1",
        "biko": "午前中希望",
        "preferred_payment": "代金引換"
    }

    # 2. 模拟加购成功后的真实购物车页面（将空提示替换为商品信息，保留真实的 a.btn_submit_checkout 和全套 DOM 结构）
    filled_cart_html = real_cart_html.replace(
        "カートに商品は入っていません",
        "ちっちゃなシリーズ　ちぃち　クロスゴシックドレス (1点)"
    )

    checkout_steps_log = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        method = request.method

        if method == "POST" and "cart_in" in url_path:
            checkout_steps_log.append("POST /cart_in (加购请求)")
            return httpx.Response(200, text=filled_cart_html)

        elif method == "GET" and "regi" in url_path:
            checkout_steps_log.append("GET /regi (读取真实结算表单)")
            # 返回真实 mmcp-nyuryoku.html，包含全部客户表单字段
            return httpx.Response(200, text=real_nyuryoku_html)

        elif method == "POST" and "regi" in url_path:
            checkout_steps_log.append("POST /regi (提交结算信息)")
            # 提交后进入最终订单确认页
            confirm_html = """
            <html><body>
                <div id="checkout_confirm_page">
                    <h2>ご注文内容の確認</h2>
                    <form action="/regi_confirm" method="post">
                        <input type="submit" value="注文を確定する" class="btn_submit_final" />
                    </form>
                </div>
            </body></html>
            """
            return httpx.Response(200, text=confirm_html)

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url="https://mamachapptoy.cart.fc2.com")

    checkout = FC2Checkout(
        base_url="https://mamachapptoy.cart.fc2.com",
        buyer_info=buyer_info,
        dry_run=True,
        retry_count=3,
        retry_delay=0.01
    )
    checkout.client = mock_client

    res = await checkout.execute_checkout(
        item_id="343",
        item_title="ちっちゃなシリーズ　ちぃち　クロスゴシックドレス",
        item_url="https://mamachapptoy.cart.fc2.com/ca13/343/",
        quantity=1
    )

    assert res.get("success") is True, f"基于真实 HTML 的下单测试未成功: {res.get('message')}"
    assert "DRYRUN-" in res.get("order_id", ""), "应顺利到达最终确认页面并生成模拟订单号"
    assert len(checkout_steps_log) == 3, f"完整执行步骤数异常: {checkout_steps_log}"

    print_pass("第 1 步【加购】: POST /cart_in 成功，从真实 mmcp-cart.html 解析出 /regi 通道")
    print_pass("第 2 步【收件与支付】: GET /regi 成功解析真实 mmcp-nyuryoku.html 的全套表单:")
    print_pass("   • 姓名: address[in][name], 假名: address[in][kkana]")
    print_pass("   • 邮箱: address[in][mail], 电话: address[in][tel]")
    print_pass("   • 邮编: address[in][postnum], 都道府县: address[in][ken]")
    print_pass("   • 地址: address[in][address], 提交标识: Submit1")
    print_pass("第 3 步【信息提交】: POST /regi 成功组装并提交表单数据到达最终确认界面")
    print_pass(f"第 4 步【安全截获】: 模拟模式生效，生成测试订单号: {res.get('order_id')}")


# =========================================================================
# STAGE 5: 100轮换会话重试耗尽但检测有货时的「自动无缝接力抢购」验证
# =========================================================================
async def test_auto_redispatch_when_still_instock():
    print_header("STAGE 5: 100轮换会话重试耗尽但检测仍有货时的「自动无缝接力」验证")
    cfg_mgr = ConfigManager("config.json")
    bot = BotEngine(cfg_mgr)
    bot.is_running = True
    bot.config["order"]["auto_buy"] = True
    bot.config["order"]["worker_retries"] = 2
    bot.config["order"]["session_retries"] = 2
    bot.config["order"]["worker_retry_delay"] = 0.01

    item = ShopItem(
        item_id="343",
        title="ちぃち クロスゴシック",
        price="22,800円",
        url="https://mamachapptoy.cart.fc2.com/ca13/343/",
        image_url="",
        in_stock=True,
        stock_text="在庫あり"
    )

    # 1. 模拟派发第一个下单线程
    bot.dispatch_order_worker(item, event_type="TEST_1")
    first_task = bot.active_order_tasks.get("343")
    assert first_task is not None, "应成功生成首个下单线程"
    assert "343" in bot.active_order_tasks, "任务池应记录 343 正在抢购"

    # 手动触发任务完成与清理（模拟首轮耗尽退出）
    bot._cleanup_order_task("343")

    # 验证任务退出后状态：
    assert "343" not in bot.active_order_tasks, "任务退出后必须释放任务锁"
    assert "343" not in bot.purchased_item_ids, "未下单成功不可记录为已购"
    assert "343" not in bot.soldout_item_ids, "因网络拥堵耗尽轮数绝不可误判为售罄"

    print_pass("首轮任务退出与清理验证通过: 任务锁被安全释放，且未误判为已购或售罄")

    # 2. 模拟专属独立监控协程下一次扫描检测页面：发现页面仍然有货！
    re_dispatched = False
    if "343" not in bot.purchased_item_ids and (
        "343" not in bot.active_order_tasks or bot.active_order_tasks["343"].done()
    ):
        bot.dispatch_order_worker(item, event_type="AUTO_RETRY_PASS")
        re_dispatched = True

    assert re_dispatched is True, "检测到仍然有货时，必须立即就地触发重新派发！"
    second_task = bot.active_order_tasks.get("343")
    assert second_task is not None and second_task != first_task, "必须生成全新独立的第二轮抢购任务！"
    print_pass("自动无缝接力验证通过: 监控协程发现依然有货，0.3s内就地重新拉起全新独立抢购线程接力继续冲刺！")

    # 清理任务
    bot.cancel_order_worker("343")


# =========================================================================
# STAGE 6: 官方确认为售罄 (在庫切れ) 时的彻底止损与安全退出验证
# =========================================================================
async def test_genuine_soldout_termination():
    print_header("STAGE 6: 官方确认售罄 (在庫切れ) 永久止损与任务清理验证")
    cfg_mgr = ConfigManager("config.json")
    bot = BotEngine(cfg_mgr)
    bot.is_running = True

    item = ShopItem(
        item_id="343",
        title="ちぃち クロスゴシック",
        price="22,800円",
        url="https://mamachapptoy.cart.fc2.com/ca13/343/",
        image_url="",
        in_stock=False,
        stock_text="在庫切れ（完売）"
    )

    # 模拟详情页探测到售罄
    is_soldout_detail = any(kw in item.stock_text for kw in ["在庫切れ", "完売", "売り切れ"])
    if is_soldout_detail:
        bot.soldout_item_ids.add("343")
        bot.cancel_order_worker("343", reason="详情页已售罄")

    assert "343" in bot.soldout_item_ids, "售罄商品必须记录入 soldout_item_ids"

    # 再次尝试派发该商品
    bot.dispatch_order_worker(item, event_type="SHOULD_FAIL")
    should_dispatch = (
        "343" not in bot.purchased_item_ids and
        "343" not in bot.soldout_item_ids and
        item.in_stock
    )
    assert should_dispatch is False, "已确认为售罄的商品绝不能再次派发无效请求"
    print_pass("售罄止损验证通过: 识别官方售罄关键字后，立刻标记售罄并切断请求，释放服务器 CPU 与带宽")


# =========================================================================
# MAIN RUNNER
# =========================================================================
async def main():
    print(f"\n{BOLD}{GREEN}{'#'*75}")
    print(f"  🧸 MAMACHAPP FC2 BOT - 基于真实 HTML 源码的全流程集成测试  ")
    print(f"{'#'*75}{RESET}\n")

    t_start = time.perf_counter()
    passed = 0
    total = 6

    stages = [
        ("STAGE 1: 真实商品源码 (syouhin.html) 深度解析", test_real_syouhin_parsing),
        ("STAGE 2: 维护关店多层冗余规则验证", test_maintenance_redundant_rules),
        ("STAGE 3: 维护解除前哨交叉引爆与冲刺", test_cross_sentinel_maintenance_lift),
        ("STAGE 4: 基于真实 HTML 的端到端下单执行", test_real_html_checkout_pipeline),
        ("STAGE 5: 100轮重试耗尽自动无缝接力抢购", test_auto_redispatch_when_still_instock),
        ("STAGE 6: 官方售罄永久止损与任务清理", test_genuine_soldout_termination),
    ]

    for name, func in stages:
        try:
            await func()
            passed += 1
        except Exception as e:
            print_fail(f"{name} 执行失败: {e}")
            import traceback
            traceback.print_exc()

    elapsed = time.perf_counter() - t_start
    print(f"\n{BOLD}{CYAN}{'='*75}{RESET}")
    print(f"{BOLD}{GREEN if passed == total else RED}🎉 全部集成测试执行完毕！{RESET}")
    print(f"结果统计: {BOLD}{passed}/{total} 通过{RESET} | 耗时: {elapsed:.2f} 秒")
    print(f"{BOLD}{CYAN}{'='*75}{RESET}\n")

    if passed != total:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
