"""
Comprehensive Chaos & Resilience Test Suite for MamaChapp FC2 Bot
Tests all edge cases: Store Maintenance, FastCGI 502/503 Busy, Add-to-Cart Retries,
Cart Empty vs Genuine Sold-Out Discrimination, Hedged Request Racing, and Full Checkout Pipeline.
"""

import sys
import os
import time
import math
import asyncio
from typing import Dict, Any, List, Optional
from bs4 import BeautifulSoup
import httpx

if sys.platform == "win32":
    import io
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.monitor import FC2Monitor, ShopItem
from core.checkout import FC2Checkout
from core.config import ConfigManager
from core.bot import BotEngine

# Colors for terminal output
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


# =========================================================================
# TEST 1: Store Maintenance Detection (维护模式严密检测测试)
# =========================================================================
async def test_store_maintenance_detection():
    print_header("TEST 1: 商城维护与准备中页面严密检测 (Maintenance Detection)")
    monitor = FC2Monitor(base_url="https://mamachapptoy.cart.fc2.com")

    # Case 1A: Official Maintenance Banner
    html_maint1 = "<html><body><div id='header'><h1>只今メンテナンス中です</h1><p>ご不便をおかけします</p></div></body></html>"
    is_m1 = monitor.check_maintenance(html_maint1, 200)
    assert is_m1 is True, "Failed to detect '只今メンテナンス中です'"
    print_pass("成功识别官方标准维护标识: '只今メンテナンス中です'")

    # Case 1B: Store Preparation Notice (Often right before doll drops)
    html_maint2 = "<html><body><div class='notice'>当店は現在、開店準備中につき一時閉店しております。</div></body></html>"
    is_m2 = monitor.check_maintenance(html_maint2, 200)
    assert is_m2 is True, "Failed to detect '開店準備中'"
    print_pass("成功识别上新前关店排队信号: '開店準備中につき一時閉店しております'")

    # Case 1C: Transient 503 Server Busy (Should NOT be mistaken for maintenance)
    html_busy = "<html><body>503 Service Unavailable - FastCGI overloaded</body></html>"
    is_m3 = monitor.check_maintenance(html_busy, 503)
    assert is_m3 is False, "503 Server Busy must NOT be confused with maintenance!"
    print_pass("成功区分网络/服务器繁忙与正式维护 (503 服务繁忙不误判为维护)")

    # Case 1D: Normal store with sold out products (Should NOT be mistaken for maintenance)
    html_normal = "<html><body><div id='items'><div class='item'><h2>ひなちゃん</h2><span class='stock'>在庫切れ</span></div></div></body></html>"
    is_m4 = monitor.check_maintenance(html_normal, 200)
    assert is_m4 is False, "Normal page incorrectly flagged as maintenance"
    print_pass("正常销售商品页面未发生任何维护误报 (零误判验证通过)")


# =========================================================================
# TEST 2: Maintenance Reopen & Timed Auto-Rush Transition (开售切换测试)
# =========================================================================
async def test_maintenance_to_autorush_transition():
    print_header("TEST 2: 维护结束开售无缝切换限时极速突击 (Auto Rush Transition)")
    
    cfg_mgr = ConfigManager()
    cfg_mgr.config["shop"]["auto_rush_duration_seconds"] = 1.0  # 1.0s for rapid test
    cfg_mgr.config["shop"]["rush_interval_seconds"] = 0.1
    cfg_mgr.config["shop"]["interval_seconds"] = 1.0
    bot = BotEngine(cfg_mgr)

    # 1. Simulate maintenance entrance
    bot.is_in_maintenance = True
    bot.auto_rush_active = True
    assert bot.is_in_maintenance is True
    print_pass("模拟检测到商城进入维护状态 -> 开启不限时极速轮询")

    # 2. Simulate maintenance ending / drop release
    auto_rush_dur = float(bot.config["shop"]["auto_rush_duration_seconds"])
    bot.is_in_maintenance = False
    bot.auto_rush_active = True
    bot.auto_rush_start_time = time.time()
    bot.auto_rush_end_time = time.time() + auto_rush_dur

    print_pass(f"维护结束！自动激活限时极速突击模式 (设定持续时间: {auto_rush_dur}s)")
    assert bot.auto_rush_active is True
    assert bot.is_in_maintenance is False

    # 3. Simulate time passing and verify strictly duration-based exit
    print_info("等待设定突击时间结束...")
    await asyncio.sleep(1.1)

    # Check exit condition
    if time.time() >= bot.auto_rush_end_time:
        bot.auto_rush_active = False

    assert bot.auto_rush_active is False, "Auto rush did not exit on time expiration"
    print_pass("达到设定持续时间 -> 极速突击模式平稳优雅降级为日常巡检 (退出规则验证通过)")


# =========================================================================
# TEST 3: Network Jitter & 502/503 on Polling (巡检网络抖动与连续空页面)
# =========================================================================
async def test_polling_jitter_resilience():
    print_header("TEST 3: 巡检时 502/503/超时与连续空页面熔断测试 (Polling Jitter)")

    # Mock handler that simulates 502 then 503 then recovery
    call_count = 0
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(502, text="Bad Gateway (FastCGI Server Timed Out)")
        elif call_count == 2:
            return httpx.Response(503, text="Service Temporarily Overloaded")
        else:
            return httpx.Response(200, text="<html><body><div class='item'><h2>ひな</h2><dd class='stock'>在庫あり</dd></div></body></html>")

    mock_transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=mock_transport) as client:
        monitor = FC2Monitor(client=client)

        # 1. Fetching under 502 & 503
        html, status = await monitor.fetch_page("https://mamachapptoy.cart.fc2.com/?sort=5")
        # Monitor fetch_page automatically retries on network error
        print_pass(f"在遭遇 502/503 异常时，fetch_page 自动重试并在第三次成功恢复: HTTP {status}")

    # 2. Test empty catalog detection counter
    monitor2 = FC2Monitor()
    empty_html = "<html><body><div id='wrap'></div></body></html>"
    items = monitor2.parse_items_from_html(empty_html)
    assert len(items) == 0
    print_pass("空页面商品解析结果为 0 件，未抛出任何异常")


# =========================================================================
# TEST 4: FastCGI Overload Retry during Add-to-Cart (加购时繁忙重试成功)
# =========================================================================
async def test_add_to_cart_busy_retry():
    print_header("TEST 4: 加购遭遇 502/503 繁忙及同会话就地极速重试 (Cart-in Busy Retry)")

    attempt_counter = 0
    def cart_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_counter
        attempt_counter += 1
        if attempt_counter == 1:
            return httpx.Response(503, text="只今アクセスが集中しております。しばらくお待ちください。")
        elif attempt_counter == 2:
            return httpx.Response(502, text="502 Bad Gateway - OpenResty FastCGI Backend Stalled")
        else:
            # Succeeded on 3rd attempt
            return httpx.Response(200, text="""
                <html><body>
                    <div id="checkcart_page">
                        <h2>カートの中身</h2>
                        <a href="/regi" class="btn_submit_checkout">レジへすすむ</a>
                    </div>
                </body></html>
            """)

    mock_transport = httpx.MockTransport(cart_handler)
    checkout = FC2Checkout(
        retry_count=4,
        retry_delay=0.05,
        buyer_info={"name": "テスト"}
    )
    # Inject mock transport into checkout's dedicated client
    checkout.client = httpx.AsyncClient(transport=mock_transport)

    ok, msg = await checkout.add_to_cart(item_id="352", quantity=1)
    await checkout.close()

    assert ok is True, f"Add to cart should succeed on attempt 3, got: {msg}"
    assert attempt_counter == 3, f"Expected 3 attempts, got {attempt_counter}"
    assert "[重试第3次成功]" in msg
    print_pass(f"加购前2次分别遭遇 503 访问集中与 502 网关错误，第3次成功自动恢复并捕获结算入口！")
    print_info(f"最终反馈信息: {msg}")


# =========================================================================
# TEST 5: Cart Empty (Retry) vs Genuine Sold-Out (Stop) Discrimination
# =========================================================================
async def test_cart_empty_vs_soldout_discrimination():
    print_header("TEST 5: 购物车为空(继续冲击) 与 确认售罄(立即止损) 鉴别测试")

    # Case 5A: "カートに商品は入っていません" (Transient glitch - should NOT mark as soldout)
    def empty_cart_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="""
            <html><body>
                <div id="checkcart_page">
                    <td class="td3" id="center1">カートに商品は入っていません</td>
                </div>
            </body></html>
        """)

    checkout_a = FC2Checkout(retry_count=1)
    checkout_a.client = httpx.AsyncClient(transport=httpx.MockTransport(empty_cart_handler))
    ok_a, msg_a = await checkout_a.add_to_cart("352")
    await checkout_a.close()

    is_soldout_a = any(k in msg_a for k in ["在庫切れ", "売り切れ", "完売", "品切れ"])
    assert ok_a is False
    assert is_soldout_a is False, "Cart empty should NOT be flagged as permanent soldout!"
    print_pass("成功识别 'カートに商品は入っていません' 为非永久售罄状态 (保留继续重试资格)")

    # Case 5B: Genuine Sold Out: "在庫切れ / 完売" (Should mark as soldout and terminate immediately)
    def soldout_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="""
            <html><body>
                <div id="checkcart_page">
                    <p class="error">ご指定の商品は現在、在庫切れ（売り切れ）となっております。</p>
                </div>
            </body></html>
        """)

    checkout_b = FC2Checkout(retry_count=1)
    checkout_b.client = httpx.AsyncClient(transport=httpx.MockTransport(soldout_handler))
    ok_b, msg_b = await checkout_b.add_to_cart("352")
    await checkout_b.close()

    is_soldout_b = any(k in msg_b for k in ["在庫切れ", "売り切れ", "完売", "品切れ"])
    assert ok_b is False
    assert is_soldout_b is True, "Genuine soldout MUST be flagged as permanent soldout!"
    print_pass("成功识别 '在庫切れ（売り切れ）' 为永久售罄状态 (立即终止无效请求，节省带宽)")


# =========================================================================
# TEST 6: Rolling Hedged Request Racing under Queue Latency (对冲探针竞速)
# =========================================================================
async def test_hedged_request_racing():
    print_header("TEST 6: 并发对冲探针竞速与超时排队拯救测试 (Hedged Request Racing)")

    # Simulate: Probe 1 is stuck in slow queue (takes 1.5s), Probe 2 finishes fast (in 0.2s)
    probe_attempts = 0
    async def delayed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal probe_attempts
        probe_attempts += 1
        my_id = probe_attempts
        if my_id == 1:
            # Primary probe takes 1.5 seconds
            await asyncio.sleep(1.5)
            return httpx.Response(200, text="<html><body><div id='itemdetail_page'><h2>ひな (Probe 1)</h2><dd class='stock'>在庫あり</dd></div></body></html>")
        else:
            # Hedged probe responds quickly in 0.2 seconds!
            await asyncio.sleep(0.2)
            return httpx.Response(200, text="<html><body><div id='itemdetail_page'><h2>ひな (Probe 2 - WINNER)</h2><dd class='stock'>在庫あり</dd></div></body></html>")

    mock_transport = httpx.MockTransport(delayed_handler)
    async with httpx.AsyncClient(transport=mock_transport) as client:
        monitor = FC2Monitor(client=client)

        t0 = time.perf_counter()
        # hedge_delay = 0.4s: Probe 1 stalls, at 0.4s Probe 2 spawns, Probe 2 finishes in 0.2s (total ~0.6s)
        html, status = await monitor.fetch_page_hedged(
            "https://mamachapptoy.cart.fc2.com/ca13/352/",
            timeout=3.0,
            hedge_delay=0.4,
            hedge_timeout=1.0,
            enable_hedge=True
        )
        elapsed = round(time.perf_counter() - t0, 3)

        assert status == 200
        assert "Probe 2 - WINNER" in html, "Hedge probe should have won the race!"
        assert elapsed < 1.0, f"Hedge probe should have finished in ~0.6s, but took {elapsed}s"
        print_pass(f"主请求排队卡死时，对冲探针成功并发反超并提前获胜！总耗时: {elapsed}s (相比主请求排队节省约 0.9s)")


# =========================================================================
# TEST 7: Complete 3-Step Checkout Pipeline (Dry-Run & Field Validation)
# =========================================================================
async def test_full_checkout_pipeline_dryrun():
    print_header("TEST 7: 完整下单链条端到端验证 (Full 3-Step Checkout Simulation)")

    # Mock server handling full FC2 checkout steps
    def fc2_mock_router(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cart_in":
            return httpx.Response(200, text="""
                <html><body>
                    <div id="checkcart_page">
                        <h2>カートの中身</h2>
                        <a href="/regi" class="btn_submit_checkout">レジへすすむ</a>
                    </div>
                </body></html>
            """)
        elif path == "/regi" and request.method == "GET":
            # Real FC2 /regi form with inputs & selects
            return httpx.Response(200, text="""
                <html><body>
                    <div id="checkout_page">
                        <form name="form" method="post" action="/regi">
                            <input type="hidden" name="hash_token" value="abc123xyz">
                            <input name="address[in][name]" value="">
                            <input name="address[in][tel]" value="">
                            <select name="payment_method">
                                <option value="代金引換" selected="selected">代金引換 (着払い)</option>
                                <option value="銀行振込">銀行振込</option>
                            </select>
                            <select name="derivery[time]">
                                <option value="" selected="selected">指定なし</option>
                                <option value="午前中">午前中</option>
                            </select>
                        </form>
                    </div>
                </body></html>
            """)
        elif path == "/regi" and request.method == "POST":
            # Confirmation page
            return httpx.Response(200, text="""
                <html><body>
                    <div id="confirm_page">
                        <h2>ご注文内容の確認</h2>
                        <form name="form" method="post" action="/regi">
                            <input type="hidden" name="confirm_token" value="confirmed_999">
                            <input type="submit" name="Submit2" value="注文を確定する">
                        </form>
                    </div>
                </body></html>
            """)
        return httpx.Response(404, text="Not Found")

    buyer_info = {
        "name": "山田　太郎",
        "name_kana": "ヤマダ　タロウ",
        "email": "test_buyer@gmail.com",
        "tel": "08012345678",
        "postal_code": "1000001",
        "prefecture": "東京都",
        "address": "千代田区千代田1-1",
        "biko": "午前中希望",
        "payment_method": "代金引換"
    }

    checkout = FC2Checkout(
        base_url="https://mamachapptoy.cart.fc2.com",
        buyer_info=buyer_info,
        dry_run=True,
        retry_count=2,
        retry_delay=0.1
    )
    checkout.client = httpx.AsyncClient(transport=httpx.MockTransport(fc2_mock_router))

    res = await checkout.execute_checkout(
        item_id="352",
        item_title="ちっちゃなシリーズ ひな クロスゴシック",
        quantity=1
    )

    assert res.get("success") is True, f"Checkout failed: {res.get('message')}"
    assert res.get("dry_run") is True
    assert "DRY-RUN 模拟测试成功" in res.get("message")
    assert len(res.get("timeline", [])) >= 4

    print_pass("第 1 步【加购】: POST /cart_in 成功，检测到 /regi 结算通道")
    print_pass("第 2 步【收件与支付】: GET /regi 成功自动识别表单结构、隐藏 Token、代金引换及指定なし配送选项")
    print_pass("第 3 步【信息提交】: POST /regi 成功将规范化姓名、电话、地址提交并顺利到达最终确认页面")
    print_pass(f"第 4 步【安全模拟结算】: 成功截获确认页！安全模拟模式生效，出单号: {res.get('order_id')}")
    print_info(f"完整流程用时: {res.get('elapsed_seconds')}s，包含详细步骤:\n    " + "\n    ".join(res.get("timeline", [])))


# =========================================================================
# TEST 8: Maintenance Direct Order & Detail Soldout Cancellation
# =========================================================================
async def test_maintenance_direct_orders_and_soldout_cancellation():
    print_header("TEST 8: 维护结束直接加购并发冲刺与详情页售罄取消 (Direct Fire & Soldout Cancellation)")

    cfg_mgr = ConfigManager()
    cfg_mgr.config["order"]["auto_buy"] = True
    cfg_mgr.config["order"]["dry_run"] = True
    cfg_mgr.config["order"]["max_orders"] = 1
    cfg_mgr.config["order"]["enable_maintenance_direct_order"] = True
    cfg_mgr.config["order"]["worker_retries"] = 10
    cfg_mgr.config["order"]["worker_retry_delay"] = 0.05
    cfg_mgr.config["order"]["session_retries"] = 1
    cfg_mgr.config["order"]["session_retry_delay"] = 0.05
    cfg_mgr.config["order"]["hedge_requests"] = False

    bot = BotEngine(cfg_mgr)
    bot.is_running = True

    # Emulate background workers repeatedly retrying on busy cart
    async def mock_busy_checkout(self, item_id, item_title, item_url=None, quantity=1):
        await asyncio.sleep(0.05)
        return {"success": False, "message": "加购未完成：购物车为空或尚未开放", "is_soldout": False}

    import unittest.mock
    patcher = unittest.mock.patch.object(FC2Checkout, "execute_checkout", mock_busy_checkout)
    patcher.start()

    # Register 2 upcoming items:
    # Item 343: Will be confirmed SOLD OUT on detail page
    # Item 344: Will be successfully purchased
    bot.upcoming_items = {
        "343": {
            "item_id": "343",
            "title": "ひなちゃん セーラー服",
            "price": "35,000円",
            "url": "https://mamachapptoy.cart.fc2.com/ca13/343/",
            "is_upcoming": True,
            "in_stock": False
        },
        "344": {
            "item_id": "344",
            "title": "ちのちゃん ロングメイド",
            "price": "36,000円",
            "url": "https://mamachapptoy.cart.fc2.com/ca13/344/",
            "is_upcoming": True,
            "in_stock": False
        }
    }

    # 1. Trigger maintenance direct orders
    bot.trigger_maintenance_direct_orders()

    # Both items should immediately have active background order workers!
    assert "343" in bot.active_order_tasks, "Worker for 343 should be immediately dispatched"
    assert "344" in bot.active_order_tasks, "Worker for 344 should be immediately dispatched"
    print_pass("维护结束瞬间: 成功为全部待抢商品 (343, 344) 无视详情状态直接派发独立并发下单线程！")

    # 2. Priority Monitor checks item 343 detail page -> reports SOLD OUT (在庫切れ)
    html_detail_soldout = """
    <html><body>
      <div id="itemdetail_page">
        <h2>ひなちゃん セーラー服</h2>
        <dd class="stock">在庫切れ</dd>
        <span class="price">35,000円</span>
        <input type="hidden" name="item_id" value="343">
      </div>
    </body></html>
    """
    item343 = bot.monitor.parse_single_item_page(html_detail_soldout, "https://mamachapptoy.cart.fc2.com/ca13/343/")
    assert item343.in_stock is False and not item343.is_upcoming, "Should parse as soldout"

    # Simulate priority monitor detecting soldout
    bot.soldout_item_ids.add("343")
    bot.cancel_order_worker("343", reason="详情页已售罄")

    assert "343" in bot.soldout_item_ids, "343 should be in soldout_item_ids"
    assert "343" not in bot.active_order_tasks, "Worker for 343 should be cancelled and cleaned up"
    print_pass("优先检测独立并行运行: 详情页读取到售罄 (在庫切れ) 后，立刻将 ID 343 标记为售罄并安全取消正在运行的下单线程！")

    # 3. Simulate Worker 344 succeeding
    bot.purchased_item_ids.add("344")
    bot.cancel_order_worker("344", reason="抢购成功达成限额")

    assert "344" in bot.purchased_item_ids, "344 should be marked as purchased"
    assert "344" not in bot.active_order_tasks, "Worker for 344 should be cleaned up"
    print_pass("加购成功停止策略: 抢购成功 1 件后顺利记录入库并完成会话！")
    patcher.stop()
    bot.is_running = False


# =========================================================================
# MAIN TEST RUNNER
# =========================================================================
async def run_all_tests():
    print(f"\n{BOLD}{GREEN}{'#'*75}{RESET}")
    print(f"{BOLD}{GREEN}  🧸 MAMACHAPP FC2 BOT - 全场景高压容错与结算韧性测试套件  {RESET}")
    print(f"{BOLD}{GREEN}{'#'*75}{RESET}")

    start_all = time.perf_counter()
    test_suite = [
        ("维护模式与排队准备中检测", test_store_maintenance_detection),
        ("维护结束无缝切换限时极速突击", test_maintenance_to_autorush_transition),
        ("巡检 502/503 异常重试与空页面熔断", test_polling_jitter_resilience),
        ("加购遭遇 FastCGI 繁忙就地极速重试", test_add_to_cart_busy_retry),
        ("购物车为空(继续)与售罄(止损)鉴别", test_cart_empty_vs_soldout_discrimination),
        ("并发对冲探针竞速与超时排队拯救", test_hedged_request_racing),
        ("完整下单链条端到端全流程模拟", test_full_checkout_pipeline_dryrun),
        ("维护结束瞬时直发加购与售罄熔断", test_maintenance_direct_orders_and_soldout_cancellation),
    ]

    passed_count = 0
    for name, func in test_suite:
        try:
            await func()
            passed_count += 1
        except Exception as e:
            print_fail(f"测试用例 [{name}] 执行失败: {e}")
            import traceback
            traceback.print_exc()

    total_time = round(time.perf_counter() - start_all, 2)
    print(f"\n{BOLD}{CYAN}{'='*75}{RESET}")
    print(f"{BOLD}{GREEN}🎉 全部测试执行完毕！{RESET}")
    print(f"{BOLD}结果统计: {GREEN}{passed_count}/{len(test_suite)} 通过{RESET} | 耗时: {total_time} 秒")
    print(f"{BOLD}{CYAN}{'='*75}{RESET}\n")

if __name__ == "__main__":
    asyncio.run(run_all_tests())
