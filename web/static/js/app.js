// State variables
let currentStatus = null;
let currentConfig = null;
let sseConnection = null;
let catalogItems = [];

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initSSE();
  fetchStatus();
  fetchConfig();
  initEventHandlers();

  // Polling loop for status updates every 2s
  setInterval(fetchStatus, 2000);
});

// Tab Switching
function initTabs() {
  const tabs = document.querySelectorAll(".tab-btn");
  tabs.forEach(btn => {
    btn.addEventListener("click", () => {
      tabs.forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

      btn.classList.add("active");
      const targetId = btn.getAttribute("data-tab");
      const targetPane = document.getElementById(targetId);
      if (targetPane) targetPane.classList.add("active");
    });
  });
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
      showToast(data.is_running ? "监控已启动" : "监控已暂停", "info");
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
      showToast(data.rush_mode ? "⚡ 极速抢购模式已开启 (0.8s)" : "日常巡检模式已恢复", "warn");
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
      showToast(data.dry_run ? "🛡️ 模拟模式已开启 (不实际扣款/下单)" : "⚠️ 实盘下单模式已开启！有货将直接提交订单！", data.dry_run ? "info" : "warn");
      fetchStatus();
    } catch (e) {
      showToast("切换失败: " + e, "error");
    }
  });

  // Save Config
  document.getElementById("btn-save-config").addEventListener("click", saveConfig);

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
      const data = await resp.json();
      if (resp.ok) {
        showToast(data.message || "测试通知发送成功！请检查 Discord 频道。", "success");
      } else {
        showToast(data.detail || "测试失败，请检查 Webhook 链接或网络", "error");
      }
    } catch (e) {
      showToast("测试请求失败: " + e.message, "error");
    }
  });

  // Sync Full Catalog
  const btnSync = document.getElementById("btn-sync-catalog");
  if (btnSync) {
    btnSync.addEventListener("click", async () => {
      showToast("正在同步商城全量商品（约 1~2 秒）...", "info");
      btnSync.disabled = true;
      try {
        const resp = await fetch("/api/sync-catalog", { method: "POST" });
        const data = await resp.json();
        showToast(`全量商品同步完成！共 ${data.count} 件，已按官方最新修改时间排序`, "success");
        fetchStatus();
      } catch (e) {
        showToast("同步失败: " + e.message, "error");
      } finally {
        btnSync.disabled = false;
      }
    });
  }

  // Clear Logs
  document.getElementById("btn-clear-logs").addEventListener("click", () => {
    document.getElementById("terminal-logs").innerHTML = "";
  });

  // Catalog Filter
  document.getElementById("catalog-filter").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase().trim();
    filterCatalog(q);
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

  // Maintenance banner check
  const banner = document.getElementById("maintenance-alert-banner");
  if (banner) {
    if (data.is_in_maintenance) {
      banner.style.display = "flex";
    } else {
      banner.style.display = "none";
    }
  }

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

  // Header Dry Run
  const btnDry = document.getElementById("btn-toggle-dry");
  btnDry.className = `pill-btn ${dry ? "dry-on" : "dry-off"}`;
  btnDry.querySelector(".status-indicator").textContent = dry ? "ON" : "OFF";

  // Metrics
  const badgeStatus = document.getElementById("badge-status");
  const valStatusText = document.getElementById("val-status-text");
  if (data.is_in_maintenance) {
    badgeStatus.className = "badge badge-running";
    badgeStatus.textContent = "🚨 商城维护中 (0.8s 突击)";
    valStatusText.textContent = "⚡ 发售前警戒";
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
  document.getElementById("val-total-matches").textContent = stats.total_matches;
  document.getElementById("val-orders-count").textContent = `${stats.total_orders_succeeded} / ${stats.total_orders_attempted}`;

  // Catalog
  if (data.recent_items && data.recent_items.length > 0) {
    catalogItems = data.recent_items;
    const totalCount = data.catalog_total || catalogItems.length;
    document.getElementById("catalog-count").textContent = totalCount;
    renderCatalog(catalogItems);
  }

  // History
  if (data.order_history) {
    document.getElementById("history-count").textContent = data.order_history.length;
    renderHistory(data.order_history);
  }
}

// Render Catalog
function renderCatalog(items) {
  const container = document.getElementById("catalog-items-container");
  if (!items || items.length === 0) {
    container.innerHTML = `<div class="empty-state"><p>暂无扫描到的商品</p></div>`;
    return;
  }

  container.innerHTML = items.map(item => {
    const isSoldOut = !item.in_stock;
    const stockClass = isSoldOut ? "stock-soldout" : "stock-instock";
    const stockText = isSoldOut ? "在庫切れ (售罄)" : "在庫あり (有库存)";
    const imgHtml = item.image_url 
      ? `<img src="${item.image_url}" alt="${item.title}" class="item-thumb" onerror="this.src='https://cart.fc2img.com/favicon.ico'">`
      : `<div class="item-thumb" style="display:flex;align-items:center;justify-content:center;color:#666;">🧸</div>`;

    return `
      <div class="catalog-card">
        <div class="card-top">
          ${imgHtml}
          <div class="item-info">
            <a href="${item.url}" target="_blank" class="item-title" title="${item.title}">${item.title}</a>
            <div class="item-meta">
              <span class="item-price">${item.price}</span>
              <span class="stock-tag ${stockClass}">${stockText}</span>
            </div>
          </div>
        </div>
        <div class="card-actions">
          <span class="item-id-tag">ID: ${item.item_id}</span>
          <button class="btn-test-item" onclick="openTestModal('${item.item_id}', '${encodeURIComponent(item.title)}')">
            🧪 测试下单
          </button>
        </div>
      </div>
    `;
  }).join("");
}

// Filter Catalog
function filterCatalog(query) {
  if (!query) {
    renderCatalog(catalogItems);
    return;
  }
  const filtered = catalogItems.filter(it => it.title.toLowerCase().includes(query) || it.item_id.includes(query));
  renderCatalog(filtered);
}

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
      ? `<div class="order-timeline-box">${item.timeline.map(t => `<div>• ${t}</div>`).join("")}</div>`
      : "";

    return `
      <div class="order-entry ${isSuccess ? 'success' : 'failed'}">
        <div class="order-top">
          <div style="display:flex;align-items:center;gap:10px;">
            ${tag}
            <span class="order-title">${item.item_title || '商品 ID: ' + item.item_id}</span>
          </div>
          <span class="order-timing">${item.elapsed_seconds ? item.elapsed_seconds + 's' : ''}</span>
        </div>
        <div class="order-msg">${item.message}</div>
        ${item.order_id ? `<div style="font-size:0.8rem;color:#38bdf8;">订单编号: <b>${item.order_id}</b></div>` : ''}
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
    document.getElementById("cfg-interval").value = shop.interval_seconds || 3.0;
    document.getElementById("cfg-rush-interval").value = shop.rush_interval_seconds || 0.8;

    // Order
    const order = cfg.order || {};
    document.getElementById("cfg-max-orders").value = (order.max_orders !== undefined) ? order.max_orders : 1;
    document.getElementById("cfg-quantity").value = order.quantity || 1;

    // Notifications (Discord only)
    const notifs = cfg.notifications || {};
    const dc = notifs.discord || {};
    document.getElementById("chk-dc-enable").checked = !!dc.enabled;
    document.getElementById("cfg-dc-url").value = dc.webhook_url || "";

  } catch (e) {
    console.warn("fetchConfig error:", e);
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
      interval_seconds: parseFloat(document.getElementById("cfg-interval").value) || 3.0,
      rush_interval_seconds: parseFloat(document.getElementById("cfg-rush-interval").value) || 0.8
    },
    order: {
      max_orders: isNaN(parseInt(document.getElementById("cfg-max-orders").value, 10)) ? 1 : parseInt(document.getElementById("cfg-max-orders").value, 10),
      quantity: parseInt(document.getElementById("cfg-quantity").value, 10) || 1
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
  document.getElementById("modal-test-order").classList.add("active");
};

async function triggerTestOrder(itemId, title) {
  showToast(`🧪 正在发起测试下单 (ID: ${itemId})...`, "info");
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
      showToast("测试未完成: " + res.message, "warn");
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
