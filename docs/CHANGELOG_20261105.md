# 持仓操作区功能改造 · 2026-11-05

## 一、修掉的真 BUG（不止用户报的两个）

### BUG 1 · `api.post` 不存在 —— 4 个功能同时失效

**现象**：点「调整杠杆」报错、单仓平仓报错、一键全平报错、日目标重置报错。

**根因**：`src/api.js` 里 `post` 是**独立命名导出**，但 `api` 对象里没挂 `post`：

```js
export { post }        // 单独导出
export const api = {  /* 没有 post！ */ }
```

前端调用 `api.post(...)` → `undefined is not a function`。

**修复**：`api` 对象里加 `post, get,`。

**影响面**：4 处调用全部恢复正常
- `Dashboard.jsx:70` 一键全平
- `Dashboard.jsx:78` 杠杆调整
- `Dashboard.jsx:86` 单仓平仓
- `RiskPage.jsx:83` 日目标重置

### BUG 2 · 追加保证金输入框是死的

**现象**：输入数字后没反应、无确认、不生效。

**根因**：`<input>` 只写了 `placeholder` 和样式，**没有 `value` / `onChange` / 任何事件**。纯装饰元素。

**修复**：受控组件 + 回车/失焦提交 + 二次确认弹窗。

### BUG 3 · 后端 `Body` 未导入（改造中新引入，已当场发现）

新增 `body: dict = Body(...)` 时忘加 import，Pyright 报 `"Body" is not defined`。补齐 `from fastapi import Body, FastAPI, HTTPException`。

---

## 二、新增功能

### 1. 分批平仓（全平 / 一半 / 1/3）

**后端** `executor.close_position(pos, price, reason, fraction=1.0)`：
- `fraction >= 0.999` → 原全平逻辑
- `0 < fraction < 1` → **只减仓，仓位保持 open**：
  - `qty` 按比例扣减
  - `extra_margin` 按比例扣减
  - `trades.kind = 'partial_close'` 单独记账
  - 校验：P&L 只按平掉的那部分数量算

**接口** `POST /api/positions/{pid}/close`，body `{"fraction": 0.5}`。

### 2. 所有持仓操作加二次确认

| 操作 | 确认方式 | 弹窗内容 |
|---|---|---|
| 调整杠杆 | Modal.confirm | 从 X倍→Y倍、保证金、名义价值、距爆仓% |
| 单仓平仓 | Modal 三按钮 | 数量、保证金、浮盈 + 全平/一半/1/3 |
| 追加保证金 | Modal.confirm | 追加金额、保证金变化、风险说明 |
| 一键全平 | Popconfirm（原有） | 确定全平 N 单？ |

### 3. 弹窗深色主题

antd 默认白底，在深色界面上极度突兀。`v2.css` 追加约 70 行覆盖：
- 弹窗底 `#0b1016` + 青色描边 `rgba(34,201,232,.26)`
- 标题青色 `#45e6f5`、正文 `#c8cfdd`
- 主按钮洋红 `rgba(224,74,158,.18)`、次按钮暗色描边
- 遮罩 `rgba(0,0,0,.86)`

---

## 三、验证数据

### 部分平仓数学（TIA/ATOM 实测）

```
平仓前  数量 984.2146   保证金 299.84
执行    fraction = 0.5
平仓后  数量 492.1073   保证金 149.92
校验    数量差 0.000000000   保证金差 0.0000   仓位保持 open
```

### UI 元素清点

```
平仓弹窗比例按钮  ["全平 100%", "平一半 50%", "平 1/3"]
杠杆按钮          24 个（6 仓位 × 4 档）
平仓按钮          6 个
保证金输入框      6 个
```

### 端到端（点确认后真的生效）

```
点击 10x → 弹窗「从 3倍 调整到 10倍」→ 点确认 → 日志 POST /leverage 200 → 杠杆变 10x
点击 3x  → 弹窗「从 10倍 调整到 3倍」→ 点确认 → 日志 POST /leverage 200 → 杠杆回 3x
```

---

## 四、踩的坑

### 坑 1 · `positions` 表没有 `margin` 列

第一版部分平仓写 `db.update_position(pid, margin=...)` → `sqlite3.OperationalError: no such column: margin`（500）。

**真相**：表里只有 `extra_margin`。前端显示的「保证金」是**算出来的**：`qty × entry_price / leverage`。

**教训**：改数据库字段前先 `PRAGMA table_info(positions)`，别信前端字段名。

### 坑 2 · 测试污染了 paper 盘

验证部分平仓时真的平掉了 ATOM 一半仓位，产生：
- `qty` 984 → 492
- `paper_balance` 被扣 6.44U
- `trades` 多一笔 `partial_close`

**已全部恢复**：qty 改回原值（差 0.0）、balance 加回、测试交易删除。

**教训**：在**真实数据**上测写操作，测完必须能回滚。要么先备份 db，要么记下原值。

### 坑 3 · CSS 选择器命中不了 React 内联样式

写 `.ant-modal-confirm-content div[style*="73788e"]` 想改提示文字颜色 —— React 内联 style 渲染成 `rgb(115, 120, 142)`，不是 hex，选择器不匹配。

**解法**：直接改源码里的颜色值。

---

## 五、涉及文件

| 文件 | 改动 |
|---|---|
| `frontend-v2/src/api.js` | `api` 对象加 `post, get` |
| `frontend-v2/src/pages/Dashboard.jsx` | 三个 handler 重写 + 输入框受控 + 弹窗 JSX |
| `frontend-v2/src/v2.css` | 追加 ~70 行弹窗深色主题 |
| `backend/executor.py` | `close_position` 支持 `fraction` |
| `backend/main.py` | close 接口接受 `fraction`；补 `Body` 导入 |

---

# 附加修复 · 监控池「易插针」误杀（2026-11-05 上午）

## 现象

监控池 30 个币，**28 个被判「易插针」**，只剩 INJUSDT 一个能交易。
日志：`quality refreshed: 29 symbols, pass 1`

真正危险的不是少赚，而是**现有 6 个仓位平掉后，新信号会全部因"易插针"开不了仓，策略实际停摆**。

## 根因

`backend/quality.py` 的插针算法：

```python
spike = max(high - low for x in bars[-96:]) / atr   # 96 根 15m K线 = 24 小时
```

**问题在数据源周期**：15 分钟 K线噪音极大，96 根里每根都可能是长影线十字星。
实测 29 个币的 spike 值**全部落在 4.0 ~ 8.4**，而阈值是 3.5 —— 等于整个池子判死刑。

**不是阈值定错，是度量尺子选错了。**

## 排查过程（三轮实测，不能拍脑袋）

| 方案 | 结果 | 结论 |
|---|---|---|
| ① 15m 的 90 分位 | 所有币 1.67~2.76 | ❌ 无区分度，29/29 全过 = 没过滤 |
| ② 15m 的影线占比 / 长影线根数 | 影线占比全 ≈0.95；长影 23~42 根 | ❌ 15m 上所有币都"像插针" |
| ③ **1h 的 max振幅/ATR（96 根 = 4 天）** | **2.08 ~ 8.33，正常币 2.1~3.5** | ✅ **有区分度** |

## 修复

```python
bars  = await binance.klines(sym, "1h", 100)   # 15m → 1h
qv    = sum(volume * close for x in bars[-24:])  # 24 根 1h = 24 小时成交额
spike = max(high - low for x in bars[-96:]) / atr # 96 根 1h = 4 天窗口
MIN_ATR_PCT: 0.3 → 0.6                          # 口径从 15m 换 1h，ATR 约大 2 倍
MAX_SPIKE:   3.5 保持不变
```

**顺带修的两个口径 bug**：
- `qv` 原来用 `bars[-96:]`，在 1h 口径下会变成 4 天成交额（放大 4 倍），已改为 `bars[-24:]`
- `MIN_ATR_PCT` 未随周期换算，会导致所有币被判"死水"

## 结果

| | 修复前 | 修复后 |
|---|---|---|
| 通过币数 | **1** | **26** |
| 被拦 | 28 个「易插针」 | 3 个（见下） |

**被拦的 3 个，每一个都拦得对**：

| 币种 | 插针值 | ATR% | 成交额 | 拦截理由 |
|---|---|---|---|---|
| TRXUSDT | **8.33** | 0.27% | 58M | ✅ 低波动币（ATR 仅 0.27%），一次异动就是 8 倍 ATR —— 典型插针货 |
| ORDIUSDT | 2.54 | 1.33% | **7M** | ✅ 成交额不足 1000 万，流动性差 |
| BTCUSDT | **3.52** | 0.67% | 18370M | ⚠️ 仅超阈值 0.02，属边界情况 |

> BTC 明天波动数据一变就可能重新通过 —— **不为单个币调阈值**，避免过拟合。

## 流量影响

**零增加**。仍然每 5 分钟每币拉一次 100 根 K线，只是周期从 15m 换成 1h（数据量相同）。

## 验证

```
quality refreshed: 29 symbols, pass 26
监控池 26 个币可交易，策略恢复正常开仓能力
```

## 文件

- `backend/quality.py` — 常量注释块 + `_fetch()` 三处改动

---

## 附加改造 · 持仓操作区三项交互（用户验收版）

### 需求
1. 平仓比例（全平 / 平一半 / 平 1/3）像杠杆倍数那样**直接做在显示栏里**，不藏弹窗
2. 杠杆 / 加仓 / 平仓的二次确认，从**全屏遮罩 Modal** 改成**原位小浮窗**（一键全平那种），不要背景模糊
3. 加仓输入框填了数字后**点取消要自动归零/清空**

### 改动

**`frontend-v2/src/pages/Dashboard.jsx`**
- 删除 `Modal` 导入，改用 `Popconfirm`
- 新增浮窗状态：`const [pop, setPop] = useState(null)`，形如 `{pid, kind:'lev'|'close'|'margin', lev, frac}`
- `isPop(p, kind)` 判断某行某类浮窗是否打开；`closePop()` 统一关闭
- `closeOne(p)` 拆成三个独立动作：`askClose(p, frac)` → `doClose(p)`，每个比例按钮各自带浮窗
- `setLev` → `askLev(p, lev)` + `doLev(p)`；点当前倍数直接提示"当前已经是 N 倍"，不弹窗
- `submitMargin` → `askMargin(p)` + `doMargin(p)`；新增 `cancelMargin(p)` 取消时清空该行输入
- 平仓列：1 个「平仓」按钮 → **三个常驻按钮 `全平` / `½` / `⅓`**（全平红描边，其余灰描边）
- 加仓输入框：**去掉 `onBlur` 提交**（会和浮窗取消冲突），只保留 Enter 提交 + Esc 取消
- 三处 `description` 统一用 `.pop-h`（标题）+ `.pop-row`（标签左/数值右）+ `.pop-warn`（橙色警示）

**`frontend-v2/src/v2.css`**（1800 → 1976 行）
- 新增 `.ant-popover.pop-dark` 深色主题（背景 `#0b1016`、边框 `rgba(34,201,232,.30)`、圆角 3px）
- **关键坑**：antd Popconfirm 的真实类名是 `.ant-popconfirm-message-icon`，不是 `.ant-popover-message-icon`；写错则黄感叹号图标不消失
- `.pop-row` 用 `grid-template-columns: 42px 1fr` + `tabular-nums` 实现标签左、数字右严格对齐
- `.ant-popover.pop-dark { max-width: 290px !important }` —— antd 默认气泡只有 160px 宽，内容会挤成竖条

### 实测结果

| 浮窗 | 内容 | 尺寸 | 遮罩 |
|---|---|---|---|
| 平仓·全平 | 平仓比例 100% / 数量 984.21→0.000 / 保证金 495U→0U / 浮盈 +25.25U | 210×198 | 无 |
| 平仓·⅓ | 平仓比例 33% / 数量 984.21→656.14 / 保证金 495U→329U | 210×198 | 无 |
| 杠杆 10x | 3倍→10倍 / 保证金 495U 不变 / 名义价值 1486U / 距爆仓 34% | 210×171 | 无 |
| 加仓 50U | 追加 50U / 保证金 495U→545U | 210×157 | 无 |

- 页面不再出现 `.ant-modal-mask`（全屏遮罩），背景零模糊
- 黄色感叹号图标已隐藏（`.ant-popconfirm-message-icon { display: none }`）
- **取消后输入框清空已验证**：输入 50 → Enter → 点取消 → 值为 `""`；输入 88 → Enter → Esc → 值为 `""`

### 测试污染与恢复（教训）
浏览器自动化测试时，脚本强行改浮窗 `className` 破坏 antd 状态，误触「确认平仓」两次，把 TIA（position id=18）全平了：
- `trades id=40` partial_close 1064.27 @0.458812 pnl=-8.79
- `trades id=42` close 1064.27 @0.463603 pnl=-3.69
- 余额被扣 12.49U

恢复脚本 `restore_tia.py`：qty 回 2128.5463669633477、status 回 open、清空平仓字段、删两条交易、余额加回 12.49U。复查：6 个仓位、残留平仓单 0、余额 10116.889088934。

**教训：自动化测试按钮类的破坏性操作，永远不要用会改变 DOM class / 结构的注入方式；测试完必须核对 `positions` 数量与 `trades` 条数。**

---

## 追加 · 持仓表列宽重排 + 平仓按钮放大上色

### 问题
½ / ⅓ 按钮只有 22×16px，实际点不着。

### 改动

**`Dashboard.jsx`**
- 表格改 `tableLayout: 'fixed'` + 13 个 `<colgroup>` 百分比列宽，宽度可精确控制
- 操作列 **155px → 260px**（+68%）；其余列合计压缩 24px

| 列 | 原宽 | 新宽 | 说明 |
|---|---|---|---|
| 币种 | 78 | **66** | 4 字母够用 |
| 方向 | 62 | **47** | 单字 |
| 杠杆 | 184 | **171** | 4 档按钮 |
| 爆仓价 | 83 | **79** | |
| 策略 | 76 | **58** | 只有 "S15" |
| 开仓价 | 94 | **95** | |
| 现价 | 94 | **95** | |
| 数量 | 83 | **87** | |
| 持仓金额 | 97 | **89** | |
| 保证金 | 80 | **76** | |
| 追加保证金 | 127 | **97** | 输入框够用 |
| 浮盈 | 100 | **95** | |
| **操作** | **155** | **260** | **+105px** |

- `<span display:flex gap:6>` 替代 `inline-flex gap:2`
- 按钮改 `.cbtn / .cbtn-full / .cbtn-part` 类，`flex` 布局：全平 `1.45`、½ 和 ⅓ 各 `1`
- 平仓警示文案按比例分叉：全平「整单平仓，执行后不可撤销」/ 部分「仅平 50%，剩余继续持有」

**`v2.css`**（1977 → 2038 行）
- `.cbtn` 基础：26px 高（原 16px）、11.5px 字、圆角 3、过渡动画
- `.cbtn-full` 全平：红色渐变底 `rgba(255,77,94,.18→.06)` + 红边框 `.52`
  - hover：文字转白、底色 `.55→.28`、`box-shadow 0 0 14px rgba(255,77,94,.60)`、`translateY(-1px)`
- `.cbtn-part` ½/⅓：青色渐变底 + 青边框 `.32`、字号 14px 加粗（符号本身要够大）
  - hover：`box-shadow 0 0 12px rgba(34,201,232,.50)`、`translateY(-1px)`
- `.op-table-wrap td/th { overflow:hidden; text-overflow:ellipsis }` —— fixed 布局防内容撑破

### 实测

| 项 | 结果 |
|---|---|
| 操作列宽 | 260px |
| 全平按钮 | 94 × 26 |
| ½ / ⅓ 按钮 | 69 × 26 |
| 全平 hover | 文字 `#fff`、边框实红、发光 14px、上移 1px |
| ½ hover | 文字 `#7ff2ff`、边框实青、发光 12px、上移 1px |
| 12 个非操作列 | 无省略号、无半字、无换行 |

按钮高度 16 → 26px（+62%），刚好卡在手指/鼠标都舒服的尺寸，又没有撑高行高。

---

## 追加 · 按钮尺寸回调 30%

260px 的操作列 + 26px 高的按钮，用户反馈「有点太大了」，按 70% 缩放：

| 项 | 上一版 | 现在 | 变化 |
|---|---|---|---|
| 按钮高度 | 26px | **18px** | -31% |
| 全平宽度 | 94px | **68px** | -28% |
| ½ / ⅓ 宽度 | 69px | **50px** | -28% |
| 全平字号 | 11.5px | **10px** | |
| ½ / ⅓ 字号 | 14px | **11.5px** | |
| 操作列宽 | 260px | **196px** | -25% |

省出的 64px 按比例分给其余 12 列（每列 +4~7px），数据列不再有挤压风险：

币种 66→72、方向 47→53、杠杆 171→176、爆仓价 79→84、策略 58→63、开仓价 95→100、现价 95→100、数量 87→92、持仓金额 89→95、保证金 76→81、追加保证金 97→102、浮盈 95→100。

字号没有跟着等比缩到 8px（太小看不清），只降到 10px / 11.5px，靠 padding 从 6px 收到 4px 补足空间。

---

## 追加 · 插针阈值放宽 + 「追加保证金」改名

### 1. 插针阈值 3.5 → 4.0

**`backend/quality.py`**
```python
MAX_SPIKE = 4.0       # 原 3.5
```

**为什么改**：插针比 = `4天内单根1hK线最大振幅 ÷ ATR22`，这个指标对**低波动大市值币系统性偏高** —— BTC 的 ATR 只有 0.64%，分母小，比值天然高。实测 BTC 3.70 / ETH 3.48 排在全场 30 个币的第一、第二名，而它们恰恰是流动性最好的两个标的。这在逻辑上是反的。

**实测效果**：

| 币种 | 改前（阈值 3.5） | 改后（阈值 4.0） |
|---|---|---|
| BTCUSDT | 3.70 ❌ 被拦 | **3.88 ✅ 通过** |
| ETHUSDT | 3.48 ✅ 擦边 | **3.69 ✅ 通过** |
| TRXUSDT | 8.53 ❌ | 3.16 ❌（改由「死水」拦，ATR 0.24%） |
| ORDIUSDT | 2.65 ❌ 量小 | 2.65 ❌ 量小（不变） |
| 通过数 | 26 / 30 | **27 / 30** |

**关键**：放宽后放行的是真正该放行的（BTC/ETH），拦住的仍是真正有问题的（TRX ATR 0.24% 死水、ORDI 成交额 700 万）。区分度没有被破坏。

### 2. 「追加保证金」→「补保证金」

原列名和功能不符：它做的是"补保证金降低杠杆"，**不改变持仓数量**，叫「追加保证金」容易被理解成加仓。

| 位置 | 改前 | 改后 |
|---|---|---|
| 表头 | 追加保证金 | **补保证金** |
| 输入框 placeholder | + 加仓 | **+ 补保** |
| 输入框 tooltip | 追加保证金（回车确认） | **补保证金降杠杆（不会增加持仓数量，回车确认）** |
| 浮窗标题 | XXX · 追加保证金 | **XXX · 补保证金** |
| i18n `dash.col.addMargin` | 追加保证金 | **补保证金** |
| i18n `pos.marginBtn` | 追加 | **补** |
| i18n `pos.marginOk` | 保证金已追加 | **保证金已补入** |
| i18n `pos.marginFail` | 追加失败 | **补保证金失败** |
| i18n `pos.marginNeedAmt` | 请输入追加金额 | **请输入补入金额** |

**i18n 的 key 名（`addMargin`）没动** —— 只改显示值，避免影响其他引用。

### 实测
- 表头 13 列中第 11 列 = 「补保证金」，「追加保证金」已完全消失
- 输入框 placeholder = 「+ 补保」，宽度 62px 显示完整
- tooltip 已写明「不会增加持仓数量」，消除歧义
- 27 / 30 币通过监控池，BTC/ETH 均在内

---

## 三层池架构上线（候选池 100 → 监控池 30 → 持仓 6）

### 背景
原名单写死 30 个币，其中 20 个已掉出成交额前 41（TIA 排名 121、ATOM 158、ORDI 238），
而新起来的 ZEC(24 亿)、HYPE(8.9 亿)、CLANKER(4.7 亿) 一个都没跟。市场热点在换，名单没跟着换。

### 新增文件
- `backend/pool.py`（新增，约 250 行）—— 每日候选池构建
- `data/candidates.json` —— 候选池落盘
- `docs/三层池架构.md` —— 完整设计文档

### 改动文件
- `backend/quality.py`：REFRESH_SEC 300→3600；新增 MONITOR_SIZE=30、`_monitor` 席位、`configure()` 热配置
- `backend/config.py`：新增 `pools()`；`load_strategies()` 让 UNIVERSE 优先取候选池
- `backend/main.py`：启动时 `pool.load()` + 两个新后台任务；新增 3 个 API
- `strategies.yaml`：新增 `pools:` 段（candidate / monitor / position）
- `frontend-v2/src/api.js`：+candidates / rebuildCandidates / setWhitelist
- `frontend-v2/src/pages/ConfigPage.jsx`：「全局监控币种」→「每天候选币池」
- `frontend-v2/src/panels/PoolTable.jsx`：三层规模标题 +「池」列 + 排序

### 候选池筛选规则（每天凌晨 3 点）
基础门：`underlyingType=COIN` + `quoteAsset=USDT` + `contractType=PERPETUAL` + `status=TRADING`
波动门：30 天振幅 ∈ [10%, 300%]
组成：白名单优先 + 成交额排名补满 100

**振幅阈值实测依据**（200 个币样本）：中位 62%，P10=28.9%，P90=357%
- 下限 10%：必须低于 BTC 的 13.0% 和 BNB 的 15.1%（前 100 名里最低的两个真币）；
  刚好剔除 PAXG(8.3%)、XAUT(8.7%)、USDC(0.3%)
- 上限 300%：剔除 AKE(1745%, -90.6%)、AIN(1061%, +669%)、Q(1037%, 日波动54%)、JCT(599%, +355%)

**实测**：全市场真币 519 个 → 扫描 111 个 → 剔除 13 个 → 留 100 只，耗时 12.7 秒。

### 监控池改动
三道门不变（成交额 1000万 / 插针 4.0 / ATR 0.6%），新增：
- 席位竞争：过门的按成交额降序取前 30
- 刷新：5 分钟 → 1 小时（与 1h K 线粒度对齐）

实测：100 只里 88 个过门，抢 30 席，第 31 名 ARB(0.74亿) 起落选。

**流量**：改前 30币×5分钟 = 44.7 MB/天 → 改后 100币×1小时 = 12.4 MB/天，**省 72%**。

### 边界行为（已实测验证）
`quality.is_tradable()` 全代码库只在 `engine.py:166` 被调用一次（信号评估阶段）。
持仓管理 `position_loop` 不经过它 —— 6 个持仓里 ATOM/TIA/POL/ARB 都没进监控池，
但止损止盈照常。**池子只管能不能开新仓，不管已开的仓。**

### 新增 API
- `GET  /api/pool/candidates` — 候选池详情
- `POST /api/pool/candidates/rebuild` — 手动重建
- `POST /api/pool/whitelist` — 设置自选合约
- `GET  /api/pool` 扩展：`candidate_n` / `monitor_n` / `position_n` / `in_monitor` / `monitor`

### 界面
- 配置页「每天候选币池」：白名单输入框（tag 模式）+ 保存/重建按钮 + 漏斗展示 + 100 个币种标签
- 监控池表格标题：`候选 100 → 监控 30 → 持仓 6`
- 新增「池」列：绿色「持仓」/ 青色「监控」/ 灰色「候选」，可筛选
- 排序：持仓 → 监控内 → 候选外，同组按成交额降序
- 插针列阈值不再硬编码 3.5，跟随 `pools.monitor.max_spike`

---

## 全项目健康检查与修复

三层池上线后的深度检查，详见 `docs/健康检查_20261105.md`。

### 🔴 修复的严重问题

**1. 持仓币掉出候选池 → 止损止盈完全失效**
`f_mark_all` 只缓存 `config.UNIVERSE` 里的币。候选池每天重建，持仓币成交额下滑掉出去后
拿不到 mark 价，`manage_positions` 里 `if not price: continue` 直接跳过 → 止损裸奔。
改为缓存 `UNIVERSE ∪ 当前持仓币`。（`backend/factors.py`）

**2. WebSocket 逐笔订阅 → 21.7 GB/天流量**
`ws.py` 订阅 `@bookTicker`，候选池扩到 100 只后实测 39,842 条消息 / 30 秒 = 21.71 GB/天。
唯一需要 tick 数据的 S11 策略是关闭的，数据没人用。
重写为 REST 轮询持仓币标记价，每 5 秒一次 → **7.3 MB/天，降 2973 倍**。
（`backend/ws.py` 全量重写）

**3. 数据库无人清理**
`db.prune()` 定义了但全项目零调用，`factor_history` 已 42.7 MB / 55,665 行。
增强 prune（返回行数、参数化天数、明确不动交易资产）+ 启动跑一次 + `_prune_loop` 每天跑。
（`backend/db.py`、`backend/main.py`）

**4. 监控池刷新窗口期 `_monitor` 为空**
`refresh()` 边采边写，中间 `_cache` 已更新但 `_monitor` 还是旧值 → 误拦开仓。
改为「先算后换」原子替换。（`backend/quality.py`）

**5. 监控池刷新要 80 秒**
`refresh()` 串行拉 100 币 K 线，实测启动后 80 秒监控池才就绪，期间质检完全失效。
改为 `asyncio.gather` + `Semaphore(8)` 并发 → **80 秒降到 6 秒**。（`backend/quality.py`）

**6. `/api/pool` 看不到掉出候选池的持仓币**
遍历 `config.UNIVERSE`，持仓币不在其中就不返回 → 前端表格看不到它。
改为 `UNIVERSE ∪ 持仓币` + `in_candidate` 字段；前端「池」列显示「持仓·出池」。
（`backend/main.py`、`frontend-v2/src/panels/PoolTable.jsx`）

### 🟢 清理的冗余

- `logs/server.log.old`（25.7 MB）、`logs/server.log.test`、`strategies.yaml.bak_*`
- `frontend-v2/src/assets/`（空目录）
- `quality.monitor_symbols()`、`exchanges.bitget_symbol()`（零调用）
- `pool.py` 未使用的 `exc` 变量
- `pool._secs_until_next_run()` 死代码（有行被下一行覆盖，月末 `day+1` 会异常）
- `quality.py` 过时注释（还在写阈值 3.5）
- `.gitignore` 路径错误（写 `frontend/`，实际 `frontend-v2/`）

### 保留未动的

`binance.funding_history()` / `open_interest()`（标准 API 封装备用）、
`factors.f_*` 56 个因子（通过 REGISTRY 元组表注册）、
`backend/backtest/`（独立回测模块）、
`chandelier-zlsma-optimized.pine`（S15 翻译依据）、
`run.sh`（Linux 入口）、
`factor_history` 历史数据（prune 会 14 天后自动清）

### 验证

- `compileall` 零错误、`pyflakes` 零警告、`npm run build` 通过
- 前端 6 个页面（总览/策略库/因子库/交易日志/风控/配置）全部正常渲染，无 JS 错误
- 监控池刷新 6 秒就绪，过门 87 / 监控 30 / 持仓 6
- 6 个持仓币价格全部实时可读
- 日志零 ERROR
