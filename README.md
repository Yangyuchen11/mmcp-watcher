# 🧸 MamaChapp FC2 监控上新与极速自动下单系统

专为 **ママチャップトイ (Mamachapp Toy) FC2 官方商城** (`https://mamachapptoy.cart.fc2.com`) 设计的高性能上新监控与自动秒杀下单机器人。

针对 FC2 Cart 系统专门定制，完美解析 `/cart_in` 加购与 `/regi` 结算表单，支持部署在腾讯云轻量应用服务器（通过 **Dokploy** 面板容器化运行），并自带高颜值现代暗黑控制台。

---

## 🌟 核心特性

1. **毫秒级极速抢购流水线 (FC2 High-Speed Engine)**：
   - **Step 1 (加购)**：直发 `POST /cart_in`，自动携带 `Submit_cart=true` 与 `item_id`。
   - **Step 2 (结算)**：自动解析 `/regi` 页面可选的支付方式（优先匹配店铺特约的 `代金引換` ゆうパック COD）。
   - **Step 3 (收件人表单)**：全自动填充姓名、片假名、邮箱、电话、邮编、都道府县、详细地址及备注。
   - **Step 4 (最终确认)**：自动捕获确认页所有隐藏域 Token 与提交按钮并完成终审提交。
2. **安全模拟模式 (Dry-Run Mode)**：
   - 默认开启**模拟下单模式**。真实走完加购与收件信息校验，在到达最终扣款/COD提交页前自动刹车，安全验证抢购流程而绝不会误扣款或误产生真实订单。
3. **两种巡检模式随意切换**：
   - **日常巡检**：默认 3.0 秒间隔，低频巡检不占用带宽与被限制。
   - **极速抢购模式 (Rush Mode)**：上新前一键开启 0.8 秒（可调至 0.3~0.5s）高频雷达扫描，第一时间捕捉上新与补货瞬间。
4. **Discord Webhook 实时通知**：
   - 上新、补货与下单结果毫秒级推送至 Discord 频道（支持富文本卡片 Embed 与直达链接）。
5. **现代暗黑仪表盘 (Web Dashboard)**：
   - 实时查看当前商城商品库存状态（自动展示商品缩略图、价格、售罄/在库徽章）。
   - 一键对货架任意商品发起测试下单（Dry-run）。
   - 网页端直连 SSE (Server-Sent Events) 实时终端日志流。
   - 可视化修改收件人档案、监控关键词与通知 Key，无需手动修改代码。

---

## 🏗️ 目录结构

```text
mmcp/
├── core/
│   ├── config.py          # 配置管理器 (config.json)
│   ├── monitor.py         # 商城目录解析器与上新/补货比对引擎
│   ├── checkout.py        # 极速下单流水线 (cart_in -> regi -> confirm)
│   └── bot.py             # 后台调度机器人核心
├── notifiers/
│   ├── base.py            # 通知接口基类
│   └── discord.py         # Discord Webhook 通知
├── web/
│   ├── app.py             # FastAPI 控制面板后端 API
│   ├── templates/
│   │   └── index.html     # 高颜值暗黑主题前端页面
│   └── static/
│       ├── css/style.css  # 现代化响应式样式表
│       └── js/app.js      # 前端交互与实时日志 SSE 客户端
├── config.example.json    # 配置模板 (已预填默认收件人信息)
├── config.json            # 实际运行配置文件
├── Dockerfile             # 容器镜像构建文件
├── docker-compose.yml     # Coolify / Docker 一键编排文件
├── deploy.sh              # 快速部署脚本
├── requirements.txt       # Python 依赖包
└── run.py                 # 程序启动入口
```

---

## 🚀 部署指南：腾讯云轻量服务器 (使用 Dokploy)

根据您的服务器环境（您截图中运行的 **Dokploy** 控制面板，已部署了 `dolly-teria-watcher` 和 `Yurara-ERP`），推荐以下方式一键部署：

### 方式 A：在 Dokploy 控制面板中直接部署（最推荐）

1. **进入 Dokploy 面板**：
   - 登录您轻量服务器上的 Dokploy 控制台。
   - 进入现有 Project（项目），或点击右上角 **`+ Create Project`** 新建一个项目（例如命名为 `MMCP`）。
2. **新建 Application 服务**：
   - 在项目内点击 **`+ Create Service`** ➔ 选择 **`Application`**。
   - 服务名称填写如 `mmcp-watcher`。
3. **配置 Git 仓库源**：
   - Source 选择 **GitHub**（或 Git）。
   - 仓库地址选择：`Yangyuchen11/mmcp-watcher`。
   - 分支（Branch）：`main`。
4. **构建设置 (Build)**：
   - **Build Type**：选择 **`Dockerfile`**（或 `Docker Compose`）。
   - **Port**：填写 **`8080`**（Dokploy 的 Traefik 反向代理将自动把流量转发到此容器端口）。
   - **Domains**：添加您的自定义访问域名，并可一键开启 Let's Encrypt 自动 HTTPS 证书。
5. **持久化存储挂载 (Volumes)**（可选但推荐）：
   - 在应用的 **Volumes / Mounts** 标签页中添加挂载，防止配置在重建镜像时丢失：
     - **Host Path**：`/etc/dokploy/volumes/mmcp-watcher/config.json`
     - **Mount Path**：`/app/config.json`
6. 点击右上角 **`Deploy`**，Dokploy 将自动拉取仓库、构建镜像并启动上线！

---

### 方式 B：通过 SSH 终端使用 Docker Compose 运行

如果习惯在腾讯云轻量服务器命令行操作：

```bash
# 1. 克隆代码至服务器目录
git clone https://github.com/Yangyuchen11/mmcp-watcher.git /data/mmcp-watcher
cd /data/mmcp-watcher

# 2. 赋予执行权限并启动
chmod +x deploy.sh
./deploy.sh
```

系统将自动拉取基础镜像并在后台常驻运行，端口映射为 `8080`。

---

## 💻 本地开发与测试运行

如果需要在本地（Windows / macOS / Linux）先行测试：

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动服务
python run.py
```

启动后在浏览器打开：`http://localhost:8080` 即可进入控制台。

---

## ⚙️ 下单信息与关键配置说明

收件人数据已根据您提供的表单源码（`mmcp-nyuryoku.txt`）完成预配置：

| 字段 | 对应 FC2 表单项 | 默认值 |
| :--- | :--- | :--- |
| **姓名** | `address[in][name]` | `楊　雨晨` |
| **片假名** | `address[in][kkana]` | `ヨウウシン` |
| **邮箱** | `address[in][mail]` | `yyc285184831@gmail.com` |
| **电话** | `address[in][tel]` | `08037304421` |
| **邮编** | `address[in][postnum]` | `3510015` |
| **都道府县**| `address[in][ken]` | `埼玉県` |
| **住所** | `address[in][address]`| `朝霞市幸町`（可在 Web 端补充番地号） |
| **支付方式**| `payment_method` | `代金引換`（邮局 Yu-Pack 代收） |

> 💡 **特别提醒**：
> - **模拟模式 (Dry-Run)**：默认为打开状态。请先在控制台上点击任意商品的 **“测试下单”** 按钮，验证加购与结算表单能够完整走通。
> - **正式抢购**：在上新预定日前，于控制台顶部将 **“模拟下单模式”** 切换为 **OFF (实盘模式)**，并可打开 **“极速抢购模式”**。一旦检测到符合关键词的新品或补货，机器人将以毫秒级速度提交真实订单！
