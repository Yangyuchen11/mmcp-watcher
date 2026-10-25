// State variables
let currentStatus = null;
let currentConfig = null;
let currentDryRun = true;
let sseConnection = null;
let catalogItems = [];
let upcomingItems = [];
let activeOrders = [];
let priorityItemIds = new Set();
let catalogFilterQuery = "";
let catalogStockFilter = "all"; // 'all' | 'instock' | 'upcoming' | 'soldout'
let catalogCurrentPage = 1;
let catalogPageSize = 100;
let lastCatalogSignature = "";
let lastUpcomingSignature = "";

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initSSE();
  fetchStatus();
  fetchConfig();
  initEventHandlers();
  initIntervalControls();

  // Polling loop for status updates every 2s
  setInterval(fetchStatus, 2000);
});

// Tab Switching
function initTabs() {
  const tabs = document.querySelectorAll(".tab-btn");
  tabs.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-tab");
      switchToTab(targetId);
    });
  });
}

function switchToTab(targetId) {
  const tabs = document.querySelectorAll(".tab-btn");
  tabs.forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

  const targetBtn = document.querySelector(`.tab-btn[data-tab="${targetId}"]`);
  const targetPane = document.getElementById(targetId);
  if (targetBtn) targetBtn.classList.add("active");
  if (targetPane) targetPane.classList.add("active");
}

// Event Handlers for Header and Actions
function initEventHandlers() {
  // Start / Stop Toggle
  document.getElementById("btn-toggle-run").addEventListener("click", async () => {
    if (!currentStatus) return;
    const isRunning = currentStatus.is_running;
    const endpoint = isRunning ? "/api/stop" : "/api/start";
    try {
      const resp = await fetch(endpoint, { method: "POST" });
      const data = await resp.json();
      showToast(data.is_running ? "监控已启动！双线程扫描与并发抢购已就绪" : "监控已暂停", "info");
      fetchStatus();
    } catch (e) {
      showToast("操作失败: " + e, "error");
    }
  });

  // Rush Mode Toggle
  document.getElementById("btn-toggle-rush").addEventListener("click", async () => {
    try {
      const resp = await fetch("/api/toggle-rush", { method: "POST" });
      const data = await resp.json();
      const rushVal = document.getElementById("quick-rush-interval") ? document.getElementById("quick-rush-interval").value : "0.8";
      showToast(data.rush_mode ? `⚡ 极速抢购模式已开启 (${rushVal}s)` : "日常巡检模式已恢复", "warn");
      fetchStatus();
    } catch (e) {
      showToast("切换失败: " + e, "error");
    }
  });

  // Dry Run Toggle
  document.getElementById("btn-toggle-dry").addEventListener("click", async () => {
    try {
      const resp = await fetch("/api/toggle-dry-run", { method: "POST" });
      const data = await resp.json();
      currentDryRun = !!data.dry_run;
      showToast(data.dry_run ? "🛡️ 模拟下单模式已开启 (安全模式，不实际下单)" : "⚠️ 真实下单模式已开启！遇到有货商品将直接提交正式订单！", data.dry_run ? "info" : "warn");
      fetchStatus();
      updateCatalogView(true);
    } catch (e) {
      showToast("切换失败: " + e, "error");
    }
  });

  // Save Config (attached to all save buttons in all panels)
  document.querySelectorAll(".btn-save-config").forEach(btn => {
    btn.addEventListener("click", saveConfig);
  });
  const singleSaveBtn = document.getElementById("btn-save-config");
  if (singleSaveBtn) {
    singleSaveBtn.addEventListener("click", saveConfig);
  }

  // Reset Session
  const btnReset = document.getElementById("btn-reset-session");
  if (btnReset) {
    btnReset.addEventListener("click", async () => {
      try {
        await fetch("/api/reset-session", { method: "POST" });
        showToast("🔄 抢购会话已重置，计数与已购商品记录归零", "success");
        fetchStatus();
      } catch (e) {
        showToast("重置失败: " + e, "error");
      }
    });
  }

  // Click on Upcoming Metrics Card -> Jump to Catalog tab and filter to Upcoming
  const cardUpcoming = document.getElementById("card-metric-upcoming");
  if (cardUpcoming) {
    cardUpcoming.addEventListener("click", () => {
      switchToTab("tab-catalog");
      const btnUpcomingPill = document.querySelector('.stock-pill[data-stock="upcoming"]');
      if (btnUpcomingPill) btnUpcomingPill.click();
    });
  }

  // Test Notification
  document.getElementById("btn-test-notify").addEventListener("click", async () => {
    const url = document.getElementById("cfg-dc-url").value.trim();
    if (!url) {
      showToast("请先在下方输入框填写 Discord Webhook 链接！", "warn");
      return;
    }
    try {
      showToast("正在向 Discord 发送测试通知...", "info");
      const resp = await fetch("/api/test-notify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ webhook_url: url })
      });
      let data = {};
      const rawText = await resp.text();
      try {
        data = JSON.parse(rawText);
      } catch (err) {
        data = { detail: rawText || "服务器未返回有效内容" };
      }

      if (resp.ok && data.success !== false) {
        showToast(data.message || "测试通知发送成功！请检查 Discord 频道。", "success");
      } else {
        showToast(data.detail || data.message || "测试失败，请检查 Webhook 链接或网络", "error");
      }
    } catch (e) {
      showToast("测试请求失败: " + e.message, "error");
    }
  });

  // Sync Full Catalog
  const btnSync = document.getElementById("btn-sync-catalog");
  if (btnSync) {
    btnSync.addEventListener("click", async () => {
      showToast("正在拉取全量货架商品（约 1~2 秒）...", "info");
      btnSync.disabled = true;
      try {
        const resp = await fetch("/api/sync-catalog", { method: "POST" });
        const data = await resp.json();
        showToast(`全量商品同步完成！共 ${data.count} 件（已识别 ${data.upcoming_count || 0} 款预计上线商品）`, "success");
        lastCatalogSignature = "";
        lastUpcomingSignature = "";
        fetchStatus();
      } catch (e) {
        showToast("同步失败: " + e.message, "error");
      } finally {
        btnSync.disabled = false;
      }
    });
  }

  // Scan Upcoming Items Specifically
  const btnSyncUpcoming = document.getElementById("btn-sync-upcoming");
  if (btnSyncUpcoming) {
    btnSyncUpcoming.addEventListener("click", async () => {
      showToast("正在扫描全量页面提取所有【開始予定】商品...", "info");
      btnSyncUpcoming.disabled = true;
      try {
        const resp = await fetch("/api/upcoming/scan", { method: "POST" });
        const data = await resp.json();
        showToast(`扫描完成！共提取到 ${data.count} 款预计上线商品，已纳入优先高频扫描序列`, "success");
        upcomingItems = data.items || [];
        lastUpcomingSignature = "";
        renderUpcomingView(upcomingItems);
        fetchStatus();
      } catch (e) {
        showToast("扫描预计上线商品失败: " + e.message, "error");
      } finally {
        btnSyncUpcoming.disabled = false;
      }
    });
  }

  // Clear Logs
  document.getElementById("btn-clear-logs").addEventListener("click", () => {
    document.getElementById("terminal-logs").innerHTML = "";
  });

  // Catalog Filter Input & Clear Button
  const filterInput = document.getElementById("catalog-filter");
  const btnClearFilter = document.getElementById("btn-clear-filter");

  if (filterInput) {
    filterInput.addEventListener("input", (e) => {
      catalogFilterQuery = e.target.value;
      if (btnClearFilter) {
        btnClearFilter.style.display = catalogFilterQuery.trim() ? "flex" : "none";
      }
      catalogCurrentPage = 1;
      updateCatalogView(true);
    });
  }

  if (btnClearFilter) {
    btnClearFilter.addEventListener("click", () => {
      if (filterInput) filterInput.value = "";
      catalogFilterQuery = "";
      btnClearFilter.style.display = "none";
      catalogCurrentPage = 1;
      updateCatalogView(true);
      if (filterInput) filterInput.focus();
    });
  }

  // Stock Filter Pills
  document.querySelectorAll(".stock-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      document.querySelectorAll(".stock-pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      catalogStockFilter = pill.getAttribute("data-stock") || "all";
      catalogCurrentPage = 1;
      updateCatalogView(true);
    });
  });

  // Page Size Select
  const pageSizeSelect = document.getElementById("catalog-page-size");
  if (pageSizeSelect) {
    pageSizeSelect.addEventListener("change", (e) => {
      catalogPageSize = e.target.value;
      catalogCurrentPage = 1;
      updateCatalogView(true);
    });
  }

  // Pagination Controls Event Delegation
  document.querySelectorAll(".pagination-bar").forEach(bar => {
    bar.addEventListener("click", (e) => {
      const pageBtn = e.target.closest("[data-page]");
      if (pageBtn) {
        const pageNum = parseInt(pageBtn.getAttribute("data-page"), 10);
        if (!isNaN(pageNum)) {
          goToPage(pageNum, bar.id === "pagination-bottom");
        }
        return;
      }

      const actionBtn = e.target.closest("[data-action]");
      if (actionBtn && !actionBtn.disabled) {
        const action = actionBtn.getAttribute("data-action");
        const effectiveSize = catalogPageSize === "all" ? Math.max(catalogItems.length, 1) : (parseInt(catalogPageSize, 10) || 100);
        const totalPages = Math.max(1, Math.ceil(catalogItems.length / effectiveSize));

        if (action === "first") {
          goToPage(1, bar.id === "pagination-bottom");
        } else if (action === "prev") {
          goToPage(Math.max(1, catalogCurrentPage - 1), bar.id === "pagination-bottom");
        } else if (action === "next") {
          goToPage(catalogCurrentPage + 1, bar.id === "pagination-bottom");
        } else if (action === "last") {
          goToPage(totalPages, bar.id === "pagination-bottom");
        }
      }
    });
  });

  // Modal Actions
  const modal = document.getElementById("modal-test-order");
  const closeModal = () => modal.classList.remove("active");
  document.getElementById("btn-close-modal").addEventListener("click", closeModal);
  document.getElementById("btn-cancel-modal").addEventListener("click", closeModal);
  document.getElementById("btn-submit-modal-test").addEventListener("click", async () => {
    const itemId = document.getElementById("modal-item-id").value.trim();
    const title = document.getElementById("modal-item-name").value.trim();
    if (!itemId) {
      showToast("请输入商品 ID", "error");
      return;
    }
    closeModal();
    await triggerTestOrder(itemId, title);
  });
}

// Toggle Priority Monitoring for an Item
window.togglePriority = async function(itemId) {
  try {
    const resp = await fetch("/api/upcoming/priority", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId })
    });
    const data = await resp.json();
    if (data.success) {
      if (data.is_priority) {
        priorityItemIds.add(itemId);
        showToast(`⭐ 商品 ID ${itemId} 已设为【最高优先级监控】！`, "success");
      } else {
        priorityItemIds.delete(itemId);
        showToast(`商品 ID ${itemId} 已恢复为常态监控`, "info");
      }
      updateCatalogView(true);
      renderUpcomingView(upcomingItems);
      fetchStatus();
    }
  } catch (e) {
    showToast("操作失败: " + e.message, "error");
  }
};

// Cancel an Active Concurrent Order Worker
window.cancelActiveOrder = async function(itemId) {
  try {
    const resp = await fetch("/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId })
    });
    const data = await resp.json();
    if (data.success) {
      showToast(`🛑 已终止商品 ID ${itemId} 的并发抢购线程`, "warn");
      fetchStatus();
    } else {
      showToast(`该线程已结束或无法终止`, "info");
    }
  } catch (e) {
    showToast("取消失败: " + e.message, "error");
  }
};

// Fetch Status
async function fetchStatus() {
  try {
    const resp = await fetch("/api/status");
    if (!resp.ok) return;
    const data = await resp.json();
    currentStatus = data;
    renderStatus(data);
  } catch (e) {
    console.warn("fetchStatus error:", e);
  }
}

// Render Status UI
function renderStatus(data) {
  const isRunning = data.is_running;
  const rush = data.rush_mode;
  const dry = data.dry_run;
  const stats = data.stats;

  const curRushVal = document.getElementById("quick-rush-interval") ? document.getElementById("quick-rush-interval").value : "0.8";

  // Maintenance banner check
  const banner = document.getElementById("maintenance-alert-banner");
  if (banner) {
    banner.style.display = data.is_in_maintenance ? "flex" : "none";
    const bannerRushEl = document.getElementById("banner-rush-interval");
    if (bannerRushEl) bannerRushEl.textContent = curRushVal;
  }

  // Active Orders Banner
  activeOrders = data.active_orders || [];
  renderActiveOrdersBanner(activeOrders);

  // Header Start/Stop Button
  const btnRun = document.getElementById("btn-toggle-run");
  if (isRunning) {
    btnRun.className = "action-btn btn-danger";
    btnRun.innerHTML = `<span class="btn-icon">⏸</span><span class="btn-text">暂停监控</span>`;
  } else {
    btnRun.className = "action-btn btn-success";
    btnRun.innerHTML = `<span class="btn-icon">▶</span><span class="btn-text">启动监控</span>`;
  }

  // Header Rush Mode
  const btnRush = document.getElementById("btn-toggle-rush");
  btnRush.className = `pill-btn ${rush ? "rush-on" : "rush-off"}`;
  btnRush.querySelector(".status-indicator").textContent = rush ? "ON" : "OFF";

  // Header Dry Run / Live Order Switch
  const btnDry = document.getElementById("btn-toggle-dry");
  if (btnDry) {
    if (dry) {
      btnDry.className = "pill-btn dry-on";
      btnDry.title = "当前为模拟下单模式（不扣款），点击切换为真实下单模式";
      btnDry.innerHTML = `<span class="icon">🛡️</span><span class="label">模拟下单模式</span>`;
    } else {
      btnDry.className = "pill-btn dry-off real-order-active";
      btnDry.title = "⚠️ 当前为真实下单模式（实盘下单），点击切换为模拟下单模式";
      btnDry.innerHTML = `<span class="icon">⚡</span><span class="label">真实下单模式</span>`;
    }
  }

  // Detect Dry Run Mode Changes and Refresh Order Buttons
  if (currentDryRun !== !!dry) {
    currentDryRun = !!dry;
    updateCatalogView(true);
  }

  // Metrics
  const badgeStatus = document.getElementById("badge-status");
  const valStatusText = document.getElementById("val-status-text");
  if (data.is_in_maintenance) {
    badgeStatus.className = "badge badge-running";
    badgeStatus.textContent = `🚨 商城维护中 (${curRushVal}s 突击)`;
    valStatusText.textContent = "⚡ 发售前警戒";
  } else if (data.auto_rush_active) {
    badgeStatus.className = "badge badge-running";
    badgeStatus.textContent = `⚡ 开售极速冲刺中 (${curRushVal}s)`;
    valStatusText.textContent = "⚡ 极速突击";
  } else if (isRunning) {
    badgeStatus.className = "badge badge-running";
    badgeStatus.textContent = "🟢 运行中";
    valStatusText.textContent = rush ? "⚡ 极速突击" : "日常巡检";
  } else {
    badgeStatus.className = "badge badge-paused";
    badgeStatus.textContent = "⏸️ 已暂停";
    valStatusText.textContent = "待命中";
  }

  document.getElementById("val-last-scan").textContent = `最后扫描: ${stats.last_scan_time || "--:--:--"}`;
  document.getElementById("val-total-scans").textContent = stats.total_scans.toLocaleString();
  document.getElementById("val-scan-speed").textContent = `状态: ${stats.last_scan_status || "OK"}`;
  document.getElementById("val-orders-count").textContent = `${stats.total_orders_succeeded} / ${stats.total_orders_attempted}`;

  // Upcoming items count metric
  upcomingItems = data.upcoming_items || [];
  priorityItemIds = new Set(data.priority_item_ids || []);
  const upcomingCount = data.upcoming_count !== undefined ? data.upcoming_count : upcomingItems.length;
  
  const valUpcoming = document.getElementById("val-upcoming-count");
  if (valUpcoming) valUpcoming.textContent = `${upcomingCount} 款`;
  const badgeUpcoming = document.getElementById("badge-upcoming-metric");
  if (badgeUpcoming) badgeUpcoming.textContent = `${upcomingCount} 款`;
  const tabUpcomingBadge = document.getElementById("tab-upcoming-count");
  if (tabUpcomingBadge) tabUpcomingBadge.textContent = upcomingCount;

  // Active workers count metric
  const maxWorkers = data.config?.order?.max_concurrent_workers || 3;
  const valWorkers = document.getElementById("val-active-workers");
  if (valWorkers) valWorkers.textContent = `${activeOrders.length} / ${maxWorkers}`;
  const badgeThreads = document.getElementById("badge-threads");
  if (badgeThreads) {
    if (activeOrders.length > 0) {
      badgeThreads.className = "badge badge-running";
      badgeThreads.textContent = `抢购中 (${activeOrders.length})`;
    } else {
      badgeThreads.className = "badge badge-neutral";
      badgeThreads.textContent = "待命中";
    }
  }

  // Catalog
  if (data.recent_items && data.recent_items.length > 0) {
    catalogItems = data.recent_items;
    const totalCount = data.catalog_total || catalogItems.length;
    document.getElementById("catalog-count").textContent = totalCount;
    updateCatalogView(false);
  }

  // Render Upcoming View
  renderUpcomingView(upcomingItems);

  // History
  if (data.order_history) {
    document.getElementById("history-count").textContent = data.order_history.length;
    renderHistory(data.order_history);
  }
}

// Render Active Orders Banner (Real-time live multi-threading progress)
function renderActiveOrdersBanner(orders) {
  const banner = document.getElementById("active-orders-banner");
  const list = document.getElementById("active-orders-list");
  if (!banner || !list) return;

  if (!orders || orders.length === 0) {
    banner.style.display = "none";
    return;
  }

  banner.style.display = "flex";
  list.innerHTML = orders.map(o => `
    <div class="order-chip">
      <span class="chip-dot"></span>
      <span class="chip-title" title="${escapeHtml(o.title)}">[ID: ${o.item_id}] ${escapeHtml(o.title)}</span>
      <span class="chip-status">${escapeHtml(o.status)}</span>
      <button class="chip-cancel" onclick="cancelActiveOrder('${o.item_id}')" title="取消抢购线程">✕</button>
    </div>
  `).join("");
}

// Render Upcoming Drops Dedicated View
function renderUpcomingView(items) {
  const container = document.getElementById("upcoming-items-container");
  if (!container) return;

  const currentSig = items.map(i => `${i.item_id}_${priorityItemIds.has(i.item_id) ? 1 : 0}`).join(",");
  if (currentSig === lastUpcomingSignature) return;
  lastUpcomingSignature = currentSig;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1; padding: 40px 20px;">
        <p style="font-size: 1rem; color: #94a3b8;">📅 暂未检测到「開始予定」商品</p>
        <p style="font-size: 0.82rem; color: #64748b; margin-top: 6px;">当官方在商品后台更新为预计上线状态时将自动显示于此处并开启优先高频扫描。</p>
        <button type="button" class="btn-xs btn-primary" style="margin-top: 14px; padding: 6px 16px;" onclick="document.getElementById('btn-sync-upcoming')?.click()">🔄 立即扫描全量货架</button>
      </div>`;
    return;
  }

  container.innerHTML = items.map(item => {
    const isPriority = priorityItemIds.has(item.item_id);
    const timeLabel = item.scheduled_time || item.stock_text || "開始予定";
    const imgHtml = item.image_url 
      ? `<img src="${item.image_url}" alt="${escapeHtml(item.title)}" class="item-thumb" onerror="this.src='https://cart.fc2img.com/favicon.ico'">`
      : `<div class="item-thumb" style="display:flex;align-items:center;justify-content:center;color:#666;">🧸</div>`;

    return `
      <div class="catalog-card upcoming-card">
        <div class="card-top">
          ${imgHtml}
          <div class="item-info">
            <a href="${item.url}" target="_blank" class="item-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</a>
            <div class="item-meta">
              <span class="item-price">${item.price}</span>
              <span class="stock-tag stock-upcoming">📅 ${escapeHtml(timeLabel)}</span>
            </div>
            <div style="font-size: 0.74rem; color: #a78bfa; margin-top: 2px;">
              ⚡ 优先扫描线程毫秒级待命中
            </div>
          </div>
        </div>
        <div class="card-actions">
          <span class="badge-priority-auto">⚡ 独立优先扫描中</span>
          <div style="display:flex;align-items:center;gap:6px;margin-left:auto;">
            <span class="item-id-tag">ID: ${item.item_id}</span>
            <button class="btn-test-item ${currentDryRun ? '' : 'btn-real-order'}" onclick="openTestModal('${item.item_id}', '${encodeURIComponent(item.title)}')">
              ${currentDryRun ? '🧪 测试加购' : '⚡ 真实加购'}
            </button>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

// Catalog Fingerprint to avoid unnecessary DOM re-renders during 2s polling
function getCatalogSignature(items) {
  if (!items || items.length === 0) return "";
  return items.length + ":" + (currentDryRun ? 1 : 0) + ":" + items.map(i => `${i.item_id}_${i.in_stock ? 1 : (i.is_upcoming ? 2 : 0)}_${priorityItemIds.has(i.item_id) ? 1 : 0}`).join(",");
}

// Update Catalog View with Active Filters & Pagination
function updateCatalogView(forceRender = false) {
  const container = document.getElementById("catalog-items-container");
  if (!container) return;

  const currentSig = getCatalogSignature(catalogItems) + `_${catalogFilterQuery}_${catalogStockFilter}_${catalogCurrentPage}_${catalogPageSize}`;
  if (!forceRender && currentSig === lastCatalogSignature) {
    return;
  }
  lastCatalogSignature = currentSig;

  // 1. Update filter pill counts for all items
  const totalRaw = catalogItems.length;
  const inStockRaw = catalogItems.filter(i => !!i.in_stock).length;
  const upcomingRaw = catalogItems.filter(i => !!i.is_upcoming).length;
  const soldOutRaw = catalogItems.filter(i => !i.in_stock && !i.is_upcoming).length;

  const countAllEl = document.getElementById("count-all");
  const countInEl = document.getElementById("count-instock");
  const countUpcomingEl = document.getElementById("count-upcoming");
  const countOutEl = document.getElementById("count-soldout");

  if (countAllEl) countAllEl.textContent = totalRaw;
  if (countInEl) countInEl.textContent = inStockRaw;
  if (countUpcomingEl) countUpcomingEl.textContent = upcomingRaw;
  if (countOutEl) countOutEl.textContent = soldOutRaw;
  const totalCountIndicator = document.getElementById("catalog-total-indicator");
  if (totalCountIndicator) totalCountIndicator.textContent = totalRaw;

  // 2. Filter items by stock status
  let filtered = catalogItems;
  if (catalogStockFilter === "instock") {
    filtered = filtered.filter(i => !!i.in_stock);
  } else if (catalogStockFilter === "upcoming") {
    filtered = filtered.filter(i => !!i.is_upcoming);
  } else if (catalogStockFilter === "soldout") {
    filtered = filtered.filter(i => !i.in_stock && !i.is_upcoming);
  }

  // 3. Filter items by search query (title or item_id)
  const q = (catalogFilterQuery || "").trim().toLowerCase();
  if (q) {
    filtered = filtered.filter(i => {
      const titleMatch = (i.title || "").toLowerCase().includes(q);
      const idMatch = (i.item_id || "").toLowerCase().includes(q);
      return titleMatch || idMatch;
    });
  }

  // 4. Calculate pagination
  const filteredCount = filtered.length;
  const effectivePageSize = catalogPageSize === "all" ? Math.max(filteredCount, 1) : (parseInt(catalogPageSize, 10) || 100);
  const totalPages = Math.max(1, Math.ceil(filteredCount / effectivePageSize));

  if (catalogCurrentPage > totalPages) {
    catalogCurrentPage = totalPages;
  }
  if (catalogCurrentPage < 1) {
    catalogCurrentPage = 1;
  }

  const startIndex = (catalogCurrentPage - 1) * effectivePageSize;
  const endIndex = Math.min(startIndex + effectivePageSize, filteredCount);
  const pageItems = filteredCount > 0 ? filtered.slice(startIndex, endIndex) : [];

  // 5. Render cards
  if (filteredCount === 0) {
    if (totalRaw === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="spinner"></div>
          <p>正在同步商城商品列表...</p>
        </div>`;
    } else {
      container.innerHTML = `
        <div class="empty-state" style="grid-column: 1 / -1; padding: 40px 20px;">
          <p style="font-size: 1rem; color: #94a3b8;">🔍 未找到符合条件的商品 (全店共 ${totalRaw} 件)</p>
          <button type="button" class="btn-xs btn-ghost" style="margin-top: 14px; padding: 6px 16px;" onclick="resetCatalogFilters()">清空筛选与搜索</button>
        </div>`;
    }
  } else {
    container.innerHTML = pageItems.map(item => {
      const isUpcoming = !!item.is_upcoming;
      const isSoldOut = !item.in_stock && !item.is_upcoming;
      const isPriority = priorityItemIds.has(item.item_id);

      let cardClass = "catalog-card";
      let stockClass = "stock-soldout";
      let stockText = "在庫切れ (售罄)";

      if (isUpcoming) {
        cardClass = "catalog-card upcoming-card";
        stockClass = "stock-upcoming";
        stockText = `📅 ${item.scheduled_time || item.stock_text || "開始予定"}`;
      } else if (item.in_stock) {
        stockClass = "stock-instock";
        stockText = "在庫あり (有库存)";
      }

      const imgHtml = item.image_url 
        ? `<img src="${item.image_url}" alt="${escapeHtml(item.title)}" class="item-thumb" onerror="this.src='https://cart.fc2img.com/favicon.ico'">`
        : `<div class="item-thumb" style="display:flex;align-items:center;justify-content:center;color:#666;">🧸</div>`;

      const priorityBtn = isUpcoming 
        ? `<span class="badge-priority-auto">⚡ 优先待命</span>`
        : "";

      return `
        <div class="${cardClass}">
          <div class="card-top">
            ${imgHtml}
            <div class="item-info">
              <a href="${item.url}" target="_blank" class="item-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</a>
              <div class="item-meta">
                <span class="item-price">${item.price}</span>
                <span class="stock-tag ${stockClass}">${escapeHtml(stockText)}</span>
              </div>
            </div>
          </div>
          <div class="card-actions">
            ${priorityBtn}
            <div style="display:flex;align-items:center;gap:6px;margin-left:auto;">
              <span class="item-id-tag">ID: ${item.item_id}</span>
              <button class="btn-test-item ${currentDryRun ? '' : 'btn-real-order'}" onclick="openTestModal('${item.item_id}', '${encodeURIComponent(item.title)}')">
                ${currentDryRun ? '🧪 测试下单' : '⚡ 真实下单'}
              </button>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  // 6. Update Pagination UI
  renderPaginationUI({
    totalRaw,
    filteredCount,
    startIndex: filteredCount > 0 ? startIndex + 1 : 0,
    endIndex,
    currentPage: catalogCurrentPage,
    totalPages
  });
}

// Render Top and Bottom Pagination Bars
function renderPaginationUI(info) {
  const { totalRaw, filteredCount, startIndex, endIndex, currentPage, totalPages } = info;

  let infoText = "";
  if (filteredCount === 0) {
    infoText = `显示 <strong>0</strong> 件 / 共 <strong>${totalRaw}</strong> 件商品`;
  } else if (filteredCount === totalRaw) {
    infoText = `显示第 <strong>${startIndex}</strong> - <strong>${endIndex}</strong> 件 / 全店共 <strong>${totalRaw}</strong> 件 (第 <strong>${currentPage}</strong> / <strong>${totalPages}</strong> 页)`;
  } else {
    infoText = `显示第 <strong>${startIndex}</strong> - <strong>${endIndex}</strong> 件 / 筛选出 <strong>${filteredCount}</strong> 件 (全店共 <strong>${totalRaw}</strong> 件，第 <strong>${currentPage}</strong> / <strong>${totalPages}</strong> 页)`;
  }

  const infoTop = document.getElementById("catalog-page-info");
  const infoBottom = document.getElementById("catalog-page-info-bottom");
  if (infoTop) infoTop.innerHTML = infoText;
  if (infoBottom) infoBottom.innerHTML = infoText;

  const pageNumbers = generatePageNumbers(currentPage, totalPages);
  const pageNumsHtml = pageNumbers.map(p => {
    if (p === "...") {
      return `<span class="page-ellipsis">…</span>`;
    }
    const isActive = p === currentPage ? "active" : "";
    return `<button type="button" class="page-num-btn ${isActive}" data-page="${p}">${p}</button>`;
  }).join("");

  const pageNumbersTop = document.getElementById("catalog-page-numbers");
  const pageNumbersBottom = document.getElementById("catalog-page-numbers-bottom");
  if (pageNumbersTop) pageNumbersTop.innerHTML = pageNumsHtml;
  if (pageNumbersBottom) pageNumbersBottom.innerHTML = pageNumsHtml;

  document.querySelectorAll(".pagination-bar").forEach(bar => {
    const btnFirst = bar.querySelector('[data-action="first"]');
    const btnPrev = bar.querySelector('[data-action="prev"]');
    const btnNext = bar.querySelector('[data-action="next"]');
    const btnLast = bar.querySelector('[data-action="last"]');

    const isFirstDisabled = currentPage <= 1 || filteredCount === 0;
    const isLastDisabled = currentPage >= totalPages || filteredCount === 0;

    if (btnFirst) btnFirst.disabled = isFirstDisabled;
    if (btnPrev) btnPrev.disabled = isFirstDisabled;
    if (btnNext) btnNext.disabled = isLastDisabled;
    if (btnLast) btnLast.disabled = isLastDisabled;
  });
}

function generatePageNumbers(currentPage, totalPages) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const pages = [];
  if (currentPage <= 4) {
    for (let i = 1; i <= 5; i++) pages.push(i);
    pages.push("...");
    pages.push(totalPages);
  } else if (currentPage >= totalPages - 3) {
    pages.push(1);
    pages.push("...");
    for (let i = totalPages - 4; i <= totalPages; i++) pages.push(i);
  } else {
    pages.push(1);
    pages.push("...");
    pages.push(currentPage - 1);
    pages.push(currentPage);
    pages.push(currentPage + 1);
    pages.push("...");
    pages.push(totalPages);
  }
  return pages;
}

function goToPage(p, scrollToTop = true) {
  if (p === catalogCurrentPage) return;
  catalogCurrentPage = p;
  updateCatalogView(true);
  if (scrollToTop) {
    const catalogPane = document.getElementById("tab-catalog");
    if (catalogPane) catalogPane.scrollTo({ top: 0, behavior: "smooth" });
  }
}

window.resetCatalogFilters = function() {
  const filterInput = document.getElementById("catalog-filter");
  const btnClear = document.getElementById("btn-clear-filter");
  if (filterInput) filterInput.value = "";
  if (btnClear) btnClear.style.display = "none";
  catalogFilterQuery = "";
  catalogStockFilter = "all";
  catalogCurrentPage = 1;
  document.querySelectorAll(".stock-pill").forEach(p => {
    p.classList.toggle("active", p.getAttribute("data-stock") === "all");
  });
  updateCatalogView(true);
};

// Render History
function renderHistory(history) {
  const container = document.getElementById("history-items-container");
  if (!history || history.length === 0) {
    container.innerHTML = `<div class="empty-state">暂无下单记录</div>`;
    return;
  }

  container.innerHTML = history.map(item => {
    const isSuccess = item.success;
    const isDry = item.dry_run;
    const tag = isDry ? `<span class="badge badge-info">模拟下单</span>` : `<span class="badge ${isSuccess ? 'badge-running' : 'badge-paused'}">实盘下单</span>`;
    const timelineHtml = (item.timeline && item.timeline.length) 
      ? `<div class="order-timeline-box">${item.timeline.map(t => `<div>• ${escapeHtml(t)}</div>`).join("")}</div>`
      : "";

    return `
      <div class="order-entry ${isSuccess ? 'success' : 'failed'}">
        <div class="order-top">
          <div style="display:flex;align-items:center;gap:10px;">
            ${tag}
            <span class="order-title">${escapeHtml(item.item_title || '商品 ID: ' + item.item_id)}</span>
          </div>
          <span class="order-timing">${item.elapsed_seconds ? item.elapsed_seconds + 's' : ''}</span>
        </div>
        <div class="order-msg">${escapeHtml(item.message)}</div>
        ${item.order_id ? `<div style="font-size:0.8rem;color:#38bdf8;">订单编号: <b>${escapeHtml(item.order_id)}</b></div>` : ''}
        ${timelineHtml}
      </div>
    `;
  }).join("");
}

// Fetch & Load Configuration into Form
async function fetchConfig() {
  try {
    const resp = await fetch("/api/config");
    if (!resp.ok) return;
    const cfg = await resp.json();
    currentConfig = cfg;

    // Buyer
    const buyer = cfg.buyer || {};
    document.getElementById("cfg-name").value = buyer.name || "";
    document.getElementById("cfg-name-kana").value = buyer.name_kana || "";
    document.getElementById("cfg-email").value = buyer.email || "";
    document.getElementById("cfg-tel").value = buyer.tel || "";
    document.getElementById("cfg-postcode").value = buyer.postal_code || "";
    document.getElementById("cfg-prefecture").value = buyer.prefecture || "";
    document.getElementById("cfg-address").value = buyer.address || "";
    document.getElementById("cfg-biko").value = buyer.biko || "";
    document.getElementById("cfg-payment").value = buyer.payment_method || "代金引換";

    // Shop
    const shop = cfg.shop || {};
    document.getElementById("cfg-keywords").value = (shop.target_keywords || []).join(", ");
    document.getElementById("cfg-exclude").value = (shop.exclude_keywords || []).join(", ");
    
    const minPriceEl = document.getElementById("cfg-min-price");
    if (minPriceEl) minPriceEl.value = (shop.min_price !== undefined) ? shop.min_price : 0;
    const maxPriceEl = document.getElementById("cfg-max-price");
    if (maxPriceEl) maxPriceEl.value = (shop.max_price !== undefined) ? shop.max_price : 0;

    const normalInterval = shop.interval_seconds || 3.0;
    const rushInterval = shop.rush_interval_seconds || 0.8;
    const priorityInterval = shop.priority_interval_seconds || 0.5;

    document.getElementById("cfg-interval").value = normalInterval;
    document.getElementById("cfg-rush-interval").value = rushInterval;
    const cfgPriorityEl = document.getElementById("cfg-priority-interval");
    if (cfgPriorityEl) cfgPriorityEl.value = priorityInterval;

    const priorityRushInterval = (shop.priority_rush_interval_seconds !== undefined) ? shop.priority_rush_interval_seconds : 0.3;
    const priorityTimeout = (shop.priority_timeout_seconds !== undefined) ? shop.priority_timeout_seconds : 4.0;
    const priorityHedgeEnabled = (shop.priority_hedge_enabled !== undefined) ? !!shop.priority_hedge_enabled : true;
    const priorityHedgeDelay = (shop.priority_hedge_delay !== undefined) ? shop.priority_hedge_delay : 1.5;
    const priorityHedgeTimeout = (shop.priority_hedge_timeout_seconds !== undefined) ? shop.priority_hedge_timeout_seconds : 2.0;

    const cfgPriorityRushEl = document.getElementById("cfg-priority-rush-interval");
    if (cfgPriorityRushEl) cfgPriorityRushEl.value = priorityRushInterval;
    const cfgPriorityTimeoutEl = document.getElementById("cfg-priority-timeout");
    if (cfgPriorityTimeoutEl) cfgPriorityTimeoutEl.value = priorityTimeout;
    const chkPriorityHedgeEl = document.getElementById("chk-priority-hedge");
    if (chkPriorityHedgeEl) chkPriorityHedgeEl.checked = priorityHedgeEnabled;
    const cfgPriorityHedgeDelayEl = document.getElementById("cfg-priority-hedge-delay");
    if (cfgPriorityHedgeDelayEl) cfgPriorityHedgeDelayEl.value = priorityHedgeDelay;
    const cfgPriorityHedgeTimeoutEl = document.getElementById("cfg-priority-hedge-timeout");
    if (cfgPriorityHedgeTimeoutEl) cfgPriorityHedgeTimeoutEl.value = priorityHedgeTimeout;

    const bannerRushEl = document.getElementById("banner-rush-interval");
    if (bannerRushEl) bannerRushEl.textContent = rushInterval;

    const scanPagesEl = document.getElementById("cfg-scan-pages");
    if (scanPagesEl) scanPagesEl.value = shop.scan_pages || 2;
    const autoRushDurEl = document.getElementById("cfg-auto-rush-duration");
    if (autoRushDurEl) autoRushDurEl.value = (shop.auto_rush_duration_seconds !== undefined) ? shop.auto_rush_duration_seconds : 60;

    // Order
    const order = cfg.order || {};
    document.getElementById("cfg-max-orders").value = (order.max_orders !== undefined) ? order.max_orders : 1;
    document.getElementById("cfg-quantity").value = order.quantity || 1;
    
    const sessionRetriesEl = document.getElementById("cfg-session-retries");
    if (sessionRetriesEl) sessionRetriesEl.value = (order.session_retries !== undefined) ? order.session_retries : 3;
    const sessionRetryDelayEl = document.getElementById("cfg-session-retry-delay");
    if (sessionRetryDelayEl) sessionRetryDelayEl.value = (order.session_retry_delay !== undefined) ? order.session_retry_delay : 0.15;
    const workerRetriesEl = document.getElementById("cfg-worker-retries");
    if (workerRetriesEl) workerRetriesEl.value = (order.worker_retries !== undefined) ? order.worker_retries : 5;
    const workerRetryDelayEl = document.getElementById("cfg-worker-retry-delay");
    if (workerRetryDelayEl) workerRetryDelayEl.value = (order.worker_retry_delay !== undefined) ? order.worker_retry_delay : 0.2;
    const maxWorkersEl = document.getElementById("cfg-max-workers");
    if (maxWorkersEl) maxWorkersEl.value = (order.max_concurrent_workers !== undefined) ? order.max_concurrent_workers : 3;
    const chkHedgeEl = document.getElementById("chk-hedge-requests");
    if (chkHedgeEl) chkHedgeEl.checked = (order.hedge_requests !== undefined) ? !!order.hedge_requests : true;
    const hedgeDelayEl = document.getElementById("cfg-hedge-delay");
    if (hedgeDelayEl) hedgeDelayEl.value = (order.hedge_delay !== undefined) ? order.hedge_delay : 2.5;
    const chkMaintDirectEl = document.getElementById("chk-maintenance-direct-order");
    if (chkMaintDirectEl) chkMaintDirectEl.checked = (order.enable_maintenance_direct_order !== undefined) ? !!order.enable_maintenance_direct_order : true;

    // Notifications (Discord only)
    const notifs = cfg.notifications || {};
    const dc = notifs.discord || {};
    document.getElementById("chk-dc-enable").checked = !!dc.enabled;
    document.getElementById("cfg-dc-url").value = dc.webhook_url || "";

  } catch (e) {
    console.warn("fetchConfig error:", e);
  }
}

// Interactive Interval Controls (Sidebar Settings Live Sync)
function initIntervalControls() {
  const cfgInterval = document.getElementById("cfg-interval");
  const cfgRush = document.getElementById("cfg-rush-interval");
  const cfgPriority = document.getElementById("cfg-priority-interval");

  const syncAndSave = async (payload, msg, type = "info") => {
    try {
      const resp = await fetch("/api/update-intervals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (resp.ok) {
        showToast(msg, type);
      }
    } catch (e) {
      showToast("更新间隔失败: " + e.message, "error");
    }
  };

  const handleNormal = (val) => {
    const num = parseFloat(val);
    if (isNaN(num) || num <= 0) return;
    if (cfgInterval) cfgInterval.value = num;
    syncAndSave({ interval_seconds: num }, `⏱️ 日常巡检间隔已设为 ${num} 秒 (即时生效)`, "info");
  };

  const handleRush = (val) => {
    const num = parseFloat(val);
    if (isNaN(num) || num <= 0) return;
    if (cfgRush) cfgRush.value = num;
    const bannerRushEl = document.getElementById("banner-rush-interval");
    if (bannerRushEl) bannerRushEl.textContent = num;
    syncAndSave({ rush_interval_seconds: num }, `⚡ 极速突击间隔已设为 ${num} 秒 (即时生效)`, "warn");
  };

  const handlePriority = (val) => {
    const num = parseFloat(val);
    if (isNaN(num) || num <= 0) return;
    if (cfgPriority) cfgPriority.value = num;
    syncAndSave({ priority_interval_seconds: num }, `📅 预计上线优先扫描间隔已设为 ${num} 秒 (即时生效)`, "info");
  };

  if (cfgInterval) cfgInterval.addEventListener("change", (e) => handleNormal(e.target.value));
  if (cfgRush) cfgRush.addEventListener("change", (e) => handleRush(e.target.value));
  if (cfgPriority) cfgPriority.addEventListener("change", (e) => handlePriority(e.target.value));

  const cfgPriorityRush = document.getElementById("cfg-priority-rush-interval");
  if (cfgPriorityRush) {
    cfgPriorityRush.addEventListener("change", (e) => {
      const num = parseFloat(e.target.value);
      if (isNaN(num) || num <= 0) return;
      syncAndSave({ priority_rush_interval_seconds: num }, `⚡ 优先商品极速检测间隔已设为 ${num} 秒 (即时生效)`, "warn");
    });
  }
}

// Save Configuration
async function saveConfig() {
  const keywords = document.getElementById("cfg-keywords").value
    .split(",")
    .map(s => s.trim())
    .filter(Boolean);

  const exclude = document.getElementById("cfg-exclude").value
    .split(",")
    .map(s => s.trim())
    .filter(Boolean);

  const priorityIntervalEl = document.getElementById("cfg-priority-interval");
  const priorityRushIntervalEl = document.getElementById("cfg-priority-rush-interval");
  const priorityTimeoutEl = document.getElementById("cfg-priority-timeout");
  const chkPriorityHedgeEl = document.getElementById("chk-priority-hedge");
  const priorityHedgeDelayEl = document.getElementById("cfg-priority-hedge-delay");
  const priorityHedgeTimeoutEl = document.getElementById("cfg-priority-hedge-timeout");

  const sessionRetriesEl = document.getElementById("cfg-session-retries");
  const sessionRetryDelayEl = document.getElementById("cfg-session-retry-delay");
  const workerRetriesEl = document.getElementById("cfg-worker-retries");
  const workerRetryDelayEl = document.getElementById("cfg-worker-retry-delay");
  const maxWorkersEl = document.getElementById("cfg-max-workers");
  const chkHedgeEl = document.getElementById("chk-hedge-requests");
  const hedgeDelayEl = document.getElementById("cfg-hedge-delay");
  const chkMaintDirectEl = document.getElementById("chk-maintenance-direct-order");

  const payload = {
    buyer: {
      name: document.getElementById("cfg-name").value.trim(),
      name_kana: document.getElementById("cfg-name-kana").value.trim(),
      email: document.getElementById("cfg-email").value.trim(),
      tel: document.getElementById("cfg-tel").value.trim(),
      postal_code: document.getElementById("cfg-postcode").value.trim(),
      prefecture: document.getElementById("cfg-prefecture").value.trim(),
      address: document.getElementById("cfg-address").value.trim(),
      biko: document.getElementById("cfg-biko").value.trim(),
      payment_method: document.getElementById("cfg-payment").value.trim() || "代金引換"
    },
    shop: {
      target_keywords: keywords,
      exclude_keywords: exclude,
      min_price: parseInt(document.getElementById("cfg-min-price").value, 10) || 0,
      max_price: parseInt(document.getElementById("cfg-max-price").value, 10) || 0,
      interval_seconds: parseFloat(document.getElementById("cfg-interval").value) || 3.0,
      rush_interval_seconds: parseFloat(document.getElementById("cfg-rush-interval").value) || 0.8,
      priority_interval_seconds: priorityIntervalEl ? (parseFloat(priorityIntervalEl.value) || 0.5) : 0.5,
      priority_rush_interval_seconds: priorityRushIntervalEl ? (parseFloat(priorityRushIntervalEl.value) || 0.3) : 0.3,
      priority_timeout_seconds: priorityTimeoutEl ? (parseFloat(priorityTimeoutEl.value) || 4.0) : 4.0,
      priority_hedge_enabled: chkPriorityHedgeEl ? chkPriorityHedgeEl.checked : true,
      priority_hedge_delay: priorityHedgeDelayEl ? (parseFloat(priorityHedgeDelayEl.value) || 1.5) : 1.5,
      priority_hedge_timeout_seconds: priorityHedgeTimeoutEl ? (parseFloat(priorityHedgeTimeoutEl.value) || 2.0) : 2.0,
      scan_pages: parseInt(document.getElementById("cfg-scan-pages").value, 10) || 2,
      auto_rush_duration_seconds: document.getElementById("cfg-auto-rush-duration") ? (parseInt(document.getElementById("cfg-auto-rush-duration").value, 10) || 60) : 60
    },
    order: {
      max_orders: isNaN(parseInt(document.getElementById("cfg-max-orders").value, 10)) ? 1 : parseInt(document.getElementById("cfg-max-orders").value, 10),
      quantity: parseInt(document.getElementById("cfg-quantity").value, 10) || 1,
      session_retries: sessionRetriesEl ? (parseInt(sessionRetriesEl.value, 10) || 3) : 3,
      session_retry_delay: sessionRetryDelayEl ? (parseFloat(sessionRetryDelayEl.value) || 0.15) : 0.15,
      worker_retries: workerRetriesEl ? (parseInt(workerRetriesEl.value, 10) || 5) : 5,
      worker_retry_delay: workerRetryDelayEl ? (parseFloat(workerRetryDelayEl.value) || 0.2) : 0.2,
      max_concurrent_workers: maxWorkersEl ? (parseInt(maxWorkersEl.value, 10) || 3) : 3,
      hedge_requests: chkHedgeEl ? chkHedgeEl.checked : true,
      hedge_delay: hedgeDelayEl ? (parseFloat(hedgeDelayEl.value) || 2.5) : 2.5,
      enable_maintenance_direct_order: chkMaintDirectEl ? chkMaintDirectEl.checked : true
    },
    notifications: {
      discord: {
        enabled: document.getElementById("chk-dc-enable").checked,
        webhook_url: document.getElementById("cfg-dc-url").value.trim()
      }
    }
  };

  try {
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) throw new Error("保存失败");
    showToast("✅ 配置已成功保存并即时生效", "success");
    fetchConfig();
  } catch (e) {
    showToast("保存配置错误: " + e.message, "error");
  }
}

// SSE Logging
function initSSE() {
  if (sseConnection) {
    sseConnection.close();
  }

  sseConnection = new EventSource("/api/logs/stream");
  const terminal = document.getElementById("terminal-logs");
  const chkAutoScroll = document.getElementById("chk-auto-scroll");

  sseConnection.onmessage = (event) => {
    try {
      const entry = JSON.parse(event.data);
      appendLogLine(entry, terminal, chkAutoScroll.checked);
    } catch (e) {
      console.warn("SSE parse error:", e);
    }
  };

  sseConnection.onerror = () => {
    console.warn("SSE disconnected, reconnecting in 3s...");
    sseConnection.close();
    setTimeout(initSSE, 3000);
  };
}

function appendLogLine(entry, container, autoScroll) {
  const line = document.createElement("div");
  line.className = "log-line";
  line.innerHTML = `
    <span class="log-time">${entry.timestamp.split(" ")[1] || entry.timestamp}</span>
    <span class="log-lvl log-lvl-${entry.level}">${entry.level}</span>
    <span class="log-msg">${escapeHtml(entry.message)}</span>
  `;
  container.appendChild(line);

  // Keep max lines
  if (container.children.length > 500) {
    container.removeChild(container.firstChild);
  }

  if (autoScroll) {
    container.scrollTop = container.scrollHeight;
  }
}

// Test Order Modal
window.openTestModal = function(itemId, encodedTitle) {
  const title = decodeURIComponent(encodedTitle || "");
  document.getElementById("modal-item-id").value = itemId;
  document.getElementById("modal-item-name").value = title;

  const modalTitle = document.getElementById("modal-test-title");
  const modalDesc = document.getElementById("modal-test-desc");
  const submitBtn = document.getElementById("btn-submit-modal-test");

  if (currentDryRun) {
    if (modalTitle) modalTitle.textContent = "🧪 自定义商品下单测试";
    if (modalDesc) modalDesc.textContent = "输入商品 ID 进行完整结算链条测试（当前为模拟下单模式，不会实际扣款或生成正式订单）。";
    if (submitBtn) {
      submitBtn.textContent = "立即测试下单";
      submitBtn.className = "btn btn-primary";
    }
  } else {
    if (modalTitle) modalTitle.textContent = "⚡ 商品真实下单确认";
    if (modalDesc) modalDesc.textContent = "⚠️ 警告：当前处于【真实下单模式】！提交后将以实盘数据（收件人地址与货到付款）立即向 FC2 提交正式订单！请务必确认商品 ID！";
    if (submitBtn) {
      submitBtn.textContent = "⚡ 立即真实下单";
      submitBtn.className = "btn btn-danger";
    }
  }

  document.getElementById("modal-test-order").classList.add("active");
};

async function triggerTestOrder(itemId, title) {
  const actionName = currentDryRun ? "测试下单" : "真实下单";
  showToast(currentDryRun ? `🧪 正在发起测试下单 (ID: ${itemId})...` : `⚡ 正在发起真实下单 (ID: ${itemId})...`, currentDryRun ? "info" : "warn");
  try {
    const resp = await fetch("/api/test-checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId, title: title })
    });
    const res = await resp.json();
    if (res.success) {
      showToast(res.message, "success");
    } else {
      showToast(`${actionName}未完成: ` + res.message, "warn");
    }
    fetchStatus();
  } catch (e) {
    showToast("请求异常: " + e, "error");
  }
}

// Toast Helper
function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
