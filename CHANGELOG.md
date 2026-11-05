# 变更日志

本项目采用 [语义化版本](https://semver.org/lang/zh-CN/)。

历史详细变更记录在 `docs/` 目录，本文件只记录**里程碑级**变更。

---

## [未发布]

### 计划中（观察期结束后，2026-11-12 之后）
- Q1~Q8 优化项实施（见 `docs/优化路线_观察期.md`）
- 策略加仓能力（当前 `risk.py` 硬禁止）
- 风控口径修正：总敞口闸门纳入拟开仓、单币上限去掉杠杆乘数、熔断纳入浮亏与部分平仓盈亏、质量层改 fail-closed、事件静默加事后冷却
- 实盘前必做：模式串台修复（平仓按 `pos['mode']` 走通道）、成交价对账（用交易所 `avgPrice`）、下单确认与孤儿仓对账、部分平仓数量步进对齐

---

## [2026.11.05 补丁] - 交易链路深度审计与修复

4 个只读审计员分链路（执行 / 风控筛选 / 策略信号 / 数据 API）并行扫描，
共发现 31 条问题。本补丁只做**不改交易结果**的纯正确性修复，避免污染
观察期数据。

### 安全（新增）
- **API 访问控制中间件**：此前 696 行路由零鉴权，任意网页用一段
  `fetch(close-all)` 就能一键清仓。新增三层防护，零前端改动：
  ① Host 白名单挡 DNS rebinding；② `Sec-Fetch-Site` / `Origin` 挡 CSRF；
  ③ `QB_API_TOKEN`（可选，暴露公网时必填）

### 修复
- **K 线脏数据防护**（`binance.klines`）：任一根出现非有限值 / 价格 ≤ 0 /
  `high < low` 整轮丢弃。此前一根 `close=0` 就能把 50 根 ZLSMA 拉歪，
  NaN 会让 `stop_price` 变 NaN → `price <= stop_price` 恒为 False →
  **止损永远不触发**；NaN 写进 `qty` 会让 `paper_balance` 变 NaN →
  `equity > 0` 恒 False → **日内熔断整个失效**
- **S15 信号数值断言**：`px` / `atr` / `z` / `stop` / `tp` 逐个 `math.isfinite`
  校验，异常值丢信号而不是入库
- **缺价告警**（`engine.manage_positions`）：此前拿不到价直接 `continue`，
  无日志无告警 —— 喂价一断，该仓位的止损/止盈/移动止损/持仓时限全部
  不再执行，而系统看上去一切正常。现在按 symbol 限频 60 秒告警，
  缺价超 60 秒升级 error 并写 `meta.last_price_miss`
- **净值缺价可见**（`executor.equity`）：缺价仓位按开仓价估值的同时记录
  `meta.equity_missing_price`，避免日目标拿虚高净值误判"达标全平"
- **`entry_price` 脏值防护**：非法值跳过该仓位，不再因一次除零让
  所有仓位当轮全部不做风控

### 新增
- **看门狗**（`main._mk_supervisor`）：每 30 秒检查循环任务状态与心跳
  meta，任务异常退出或心跳停滞（position 30s / scan 120s / ws 120s）
  自动 cancel + 重建。此前循环死了但进程还活着、HTTP 还响应，
  用户以为系统在盯盘，实际风控已停摆
- **SQLite WAL 模式**：`journal_mode=wal` + `busy_timeout=8000` +
  `synchronous=NORMAL`。此前 rollback journal 下写事务阻塞所有读，
  而持仓循环每 0.5 秒读、因子采集在写，必然抢锁
- **11 个索引**：`positions(status,mode)`、`signals(ts)`、
  `trades(mode,id)`、`logs(ts)`、`factor_history(ts)` 等。
  `prune` 的 `WHERE ts<?` 此前吃不上复合索引前缀
- **HTTP 连接池复用**（`binance`）：模块级单例 `httpx.AsyncClient`，
  按事件循环检测重建。此前每个请求新建并关闭连接，10 万请求/天
  光 TLS 握手就 ~350 MB/天，是响应体本身的 40 倍。lifespan 退出时关闭

### 变更
- `.env.example` 新增 `QB_API_ALLOWED_HOSTS` / `QB_API_TOKEN` 及用法说明

---

## [2026.11.05] - 纸面主题 + 三层池上线 + 工程化收官

### 新增
- **三层币种池架构**：候选池 100（日 3:00 刷新）→ 监控池 30（每小时刷新）→ 持仓池 8
- **S15 策略**：Chandelier Exit + ZLSMA 趋势跟踪，15m 周期
- **日目标状态机**：`idle → armed → alerted → done`，支持手动重置和「今日跳过」
- **影子系统**：记录被拦截信号的事后表现，验证风控规则有效性
- **纸白主题**：可切换的双主题（玫红深色 / 纸面），含 canvas 动态背景层
- **色值体检工具**：`preview/audit_theme.js`，可复用的 WCAG 对比度扫描脚本
- **对外分享包**：`share/` 目录，含 S15 的 TradingView 版本和完整说明

### 变更
- `ws.py` 从 `bookTicker` 改为 REST 轮询：流量 21.7 GB/天 → 7.3 MB/天
- 并发上限 6 → 8，单边上限 → 6
- 插针比阈值 3.5 → 4.0，口径从 15m 改为 1h K 线
- 监控池刷新 5 分钟 → 1 小时（对齐数据粒度）
- 56 个因子采集器全部关闭，只保留 `mark_all`：流量 20-30 GB/天 → 143 MB/天

### 修复
- `f_mark_all` 缓存未包含持仓币，导致持仓币价格缺失
- `mark_price_of()` 拿不到价格时静默返回 0，POL 空单浮盈误算 +573.5U → 实际 -17.8U
- `set_global_risk` 空壳实现（调用无效）
- 日目标 `reset()` 只清状态不清 `skip_day`，下一轮 tick 立刻重新触发全平
- `/api/risk` 不返回 `daily_target`，前端标记不显示
- 数据库 `prune(14, 30)` 未接入定时任务
- 纸面模式下 315 处低对比度文字（313 处是 antd Tag 组件）
- 14 个 `.pyc` 编译产物被误纳入版本控制

### 工程化
- 补齐 GitHub 标准文件：`CONTRIBUTING.md`、`SECURITY.md`、`.editorconfig`
- 重写 `README.md`：完整说明三层池、S15 逻辑、仓位算法、风控闸门、熔断机制
- 清理 `.gitignore`，补 `preview/`、`*.bak_*`

---

## [2026.11.04] - V2 转正

### 变更
- `frontend-v2` 转正为默认前端，`frontend`（V1）归档
- 路由改为 `/?page=risk` 形式的 query 参数

详见 `docs/ARCHIVE_20261104.md`

---

## [2026.11.03] - 系统体检

### 修复
- 空头方向验证机制上线
- 交易日报 + 复盘报告 cron

详见 `docs/健康检查_20261105.md`

---

## 更早

见 `docs/CHANGELOG_20261105.md`（历史累积记录）。
