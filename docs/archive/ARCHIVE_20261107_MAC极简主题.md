# 2026-11-07 归档 · MAC 极简主题（1 黑 1 白）

> 本次工作分两段：**白天视觉第一轮**（已提交 41ea19e / 924602b）+ **夜间 MAC 极简主题**（本文档主体）。
> 只改视觉，未动任何交易/计算/接口逻辑。

---

## 一、白天视觉第一轮（已完成，此处仅索引）

| 项 | 内容 | 提交 |
|---|---|---|
| 权益曲线 | 改成「点阵底 + 弥散光晕折线」，无坐标轴，数值压左上 | `41ea19e` / `924602b` |
| 纸面回撤区对比度 | 1.34:1 → 2.83:1（白配白 → 墨色斜纹） | `41ea19e` |
| 页面宽度自适应 | board 1904 → 1664，溢出 226px → 0 | `41ea19e` |
| 行情卡下拉分组 | 持仓（置顶）→ 主流 10 → 监控池 51 | `41ea19e` |

---

## 二、夜间：MAC 极简主题

### 2.1 目标（用户原话拆解）

> 「新建两套 MAC 风格的极简主题（1 黑 1 白）…保证功能不变的情况下，只改视觉元素和视觉组件样式，新主图可以删除现有的图标和视觉元素组件，一切围绕『MAC 风格的极简主义，配色/字体/图标/组件元素/卡片框体 做到简约大气』」

- 硬红线：**功能完全不变**
- 授权：可以删现有图标 / 视觉元素组件
- 授权：自己做，不确认

### 2.2 实现路径选择

**做在现有版本上**，不复制 V3 副本。理由：

1. 现有主题机制已经是 `data-theme` 属性 + CSS 变量，加主题 = 加一组变量，不需要动结构
2. 复制副本意味着两套代码要同步维护，交易项目最怕分叉
3. 出问题可以直接 `git checkout` 回到 `924602b`，回滚成本一样低

新增文件只有 `src/mac.css` 一个；其余是变量接入和 antd token 补两套。

### 2.3 设计令牌（Design Tokens）

#### mac-dark

| 用途 | 值 |
|---|---|
| 背景 | `#000000` |
| 卡片面 | `#1c1c1e` |
| 次级面 | `#2c2c2e` |
| 三级面（轨道/衬底） | `#3a3a3c` |
| 主文字 | `#f5f5f7` |
| 次文字 | `#c7c7cc` |
| 三级文字 | `#98989d` |
| 弱文字 | `#636366` |
| 强调色 | `#0a84ff`（macOS System Blue） |
| 涨 | `#ff453a`（System Red） |
| 跌 | `#30d158`（System Green） |
| 警告 | `#ff9f0a`（System Orange） |
| 分隔线 | `rgba(255,255,255,.075)` |
| 二级分隔 | `rgba(255,255,255,.12)` |
| 阴影 | `0 1px 2px rgba(0,0,0,.55)` |
| 圆角 | 卡片 12 / 控件 8 / 小标 6 |

#### mac-light

| 用途 | 值 |
|---|---|
| 背景 | `#f5f5f7` |
| 卡片面 | `#ffffff` |
| 次级面 | `#fafafa` |
| 三级面 | `#f0f0f2` |
| 主文字 | `#1d1d1f` |
| 次文字 | `#424245` |
| 三级文字 | `#6e6e73` |
| 弱文字 | `#aeaeb2` |
| 强调色 | `#007aff` |
| 涨 | `#d70015` |
| 跌 | `#248a3d` |
| 警告 | `#c93400` |
| 分隔线 | `rgba(0,0,0,.075)` |
| 二级分隔 | `rgba(0,0,0,.12)` |
| 阴影 | `0 1px 3px rgba(0,0,0,.07)` |
| 圆角 | 同上 |

**字体**：`-apple-system, BlinkMacSystemFont, "SF Pro Text/Display", "PingFang SC", "Noto Sans SC"`。数字统一 `font-variant-numeric: tabular-nums`（金融界面数字必须等宽对齐）。

**字距**：所有 `.1em / .12em / .16em` 的「科技味宽字距」一律归零或降到 `.01~.02em`。

### 2.4 具体改动清单

#### ① 变量层（`mac.css` 第一~三节）

把 spider 的整套 CSS 变量接到 MAC 令牌上：

```css
--frame/--frame-hi/--frame-deep  → 窗口底色（原来是大面积洋红）
--bg/--panel/--panel-2           → #000 / #1c1c1e / #2c2c2e
--ink/--ink-2/--mute/--dim       → 四档文字
--cyan/--joint                    → #0a84ff（品牌青、洋红统一成系统蓝）
--good/--bad/--warn               → 系统绿 / 红 / 橙
```

这一步就让**绝大多数配色自动跟随**，因为原样式大量用 `var(--cyan)` 等。

#### ② 装饰性元素：删

| 元素 | 处理 | 原因 |
|---|---|---|
| 蛛形 logo（`.logo`） | `display: none` | 图形复杂，与极简冲突 |
| 卡通头像（`.av`） | `display: none` | 每张卡一个卡通头，噪音最大 |
| 卡通调度员（`.chief-wrap`） | `display: none` | canvas 画的彩蛋插画 |
| 雷达图（`.radar-wrap`） | `display: none` | 装饰 |
| 四角取景框（`.cn`） | `display: none` | 战术风装饰 |
| body 点阵网格 + 粉色扫描 | `background-image: none` | 满屏噪点 |
| 进度条斜纹（`.track i` / `.bar`） | 改纯色 + 圆角 | 斜纹是赛博味 |
| 所有 `text-shadow` 霓虹 | `none` | 去发光 |
| 所有 `box-shadow` 辉光 | 改 `0 1px 2px` 微阴影 | 去辉光 |

#### ③ 顶栏

- 主标题 `34px/900` → `26px/600`，字距 `-.015em`
- 副标题（`TREND FOLLOWS · RISK FIRST…`）全大写宽字距 → 正常说明文字
- 导航按钮：实心描边框 → 文字按钮 + 选中态浅色胶囊
- 时钟：黑底粉字 → 无底色等宽数字

#### ④ 卡片

- 圆角 `5px → 12px`
- 边框 `2px 洋红 → 1px rgba(255,255,255,.075)`
- 阴影：辉光 → `0 1px 2px`
- 空卡（待进场）：虚线边框 + 透明底，一眼区分「占位」与「在用」
- 币种代码 `17px/900 → 15px/600`
- 标签：洋红描边小方块 → 浅灰胶囊

#### ⑤ 图表（`EquityChart.jsx`）

`palette()` 从两分支扩到四分支。MAC 版：

- 折线 `#0a84ff` / `#007aff`，宽度 `1.7 → 1.4`
- `shadowBlur: 0`（**无发光**）
- `_halo` 光晕层 `opacity: 0`（整层不可见）
- 点阵密度降到 `size 7 / radius 0.85`，透明度 `.26 / .16`

#### ⑥ antd 组件

**关键**：antd v5 的组件底色由 `algorithm` 在 JS 层决定，CSS 覆盖不彻底。所以在 `main.jsx` 补了 `MAC_DARK_THEME`（darkAlgorithm）和 `MAC_LIGHT_THEME`（defaultAlgorithm）两套 token，并让 `ConfigProvider` 跟着 `data-theme` 切。

#### ⑦ 语义色收敛

原来一屏五套色：青 `#45e6f5` / 洋红 `#ec4fae` / 红 / 绿 / 橙。

MAC 只允许：**一个强调蓝 + 红涨绿跌 + 橙警告**，其余转灰。

落地：
- `.ant-tag-cyan/blue/purple/magenta/geekblue` → 灰胶囊
- `.ant-tag-green/red/gold` → 只留文字色 + 35% 透明描边，去掉填充块
- 空卡洋红字 → 中性灰
- Tabs：底部粗条 → 蓝色 2px 细下划线
- Segmented：改为圆角胶囊

### 2.5 技术坑（重要，下次别踩）

#### 坑 1：CSS 覆盖不生效 —— v2.css 里有多层带 `!important` 的深层选择器

例：`.spider-root .hdr .goal .track { height: 16px !important }`（4 个 class）压过 `html[data-theme] .spider-root .track`（3 个 class + 属性）。

**解法**：`.spider-root` 重复三次提权 ——

```css
html[data-theme^="mac-"] .spider-root.spider-root.spider-root .track { height: 6px !important }
```

重复 class 合法，一次多拿 2 个 class 权重，稳压 v2.css 所有深层规则。

#### 坑 2：内联样式必须回源码改

`.ant-table-thead > tr` 上有 `style="background: #0b0c12"`（`Dashboard.jsx:189`），CSS 改不动。

**解法**：改成 `background: 'var(--panel)'`，三套主题自动跟随。

同类：`App.jsx` / `Dashboard.jsx` 里 14 处写死的 `#45e6f5 / #ff7fcb / #ec4fae / #73788e / #262a39`，全部换成 `var(--cyan, 原值)` 形式（带 fallback，防止变量作用域外失效）。

#### 坑 3：`.pnl` 圆角改不动（未解）

`.pnl` 元素 computed `border-radius` 恒为 `6px`，即使插 `<style>.pnl{border-radius:40px!important}</style>` 也不变，**只有 inline + important 能改**。38 个动态样式表里找不到来源。视觉影响极小（6px 也是圆角），**暂不处理**。

#### 坑 4：antd 浅色主题下层变黑

纸面主题当初是「纸底 + 深色显示板」的混搭设计，所以只切了 CSS 没切 algorithm。MAC 浅色必须整体浅色，**必须切 `defaultAlgorithm`**，否则底部面板、表头仍是黑的。

#### 坑 5：vision 判断颜色不可靠

vision 反复报告「还有深色横条 / 空卡是灰绿」，实测：

- `.ticker` 背景 = `rgb(250,250,250)`（浅色，正常）
- `.card.idle` 背景 = `rgba(0,0,0,.01)`，边框 `rgba(0,0,0,.12)`，文字 `#aeaeb2`（全对）

**结论：颜色/对比度一律以 `getComputedStyle` + 扫描脚本为准，vision 只用来做「整体像不像」的主观判断。**

### 2.6 验收方式（可复现）

**逐元素扫描脚本**（`E:\Hermes\cache\scratch\`）：

| 脚本 | 作用 |
|---|---|
| `shot_theme.cjs <theme>` | 按主题截图 + 抓 14 项关键计算样式 |
| `dark_scan2.cjs <theme>` | 扫全页残留深色块（含渐变首个色），输出 tag/class/尺寸 |
| `color_scan.cjs` | 扫高饱和青/洋红文字，按出现次数排序 |
| `brute2.cjs` / `brute3.cjs` | 遍历全部样式表，列出命中某元素的所有圆角规则 |
| `cdp_rules.cjs` | 用 CDP `CSS.getMatchedStylesForNode` 查真实命中规则 |

**实测结果（2026-11-07 01:00）**：

```
mac-dark   bodyBg #000000  cardRadius 12px  overflow false
mac-light  bodyBg #f5f5f7  cardRadius 12px  overflow false
dark       bodyBg rgb(5,7,10)   cardRadius 5px   overflow false  ← 未破坏
paper      bodyBg rgb(238,237,232) cardRadius 5px overflow false ← 未破坏
深色块扫描：[] （零残留）
```

### 2.7 怎么用

页面右上角主题下拉，4 个选项：

```
玫红深色 → 纸面 → MAC 深色 → MAC 浅色
```

选择记在 `localStorage['quantbot.theme']`，刷新保持。默认是「玫红深色」。

### 2.8 已知遗留

1. **`.pnl` 圆角 6px 改不动**（见坑 3），视觉影响可忽略。
2. **底部表格字号偏小**（10~10.5px）—— 这是原有信息密度设计，放大可能撑破布局。MAC 版没动字号，只改了颜色/字重/字距。
3. **K 线图配色未跟随 MAC 主题** —— `PriceChart.jsx` 用的是 ECharts 默认红绿，与整体风格不算冲突（红涨绿跌是交易语义），未改。
4. **`curR >= 2` 前端写死**（旧遗留，与本次无关）。
5. **信息流默认标签页**（旧遗留，与本次无关）。

---

## 三、本次改动文件

| 文件 | 改动 |
|---|---|
| `frontend-v2/src/mac.css` | **新增** 约 700 行，两套 MAC 主题全部样式 |
| `frontend-v2/src/main.jsx` | 加 `mac.css` 引入；加 `MAC_DARK_THEME` / `MAC_LIGHT_THEME` 两套 antd token；`ConfigProvider` 四路选择 |
| `frontend-v2/src/ThemeSwitch.jsx` | 两态切换 → 四态下拉（`THEMES` 常量 + antd `Select`） |
| `frontend-v2/src/panels/EquityChart.jsx` | `palette()` 两分支扩四分支；MAC 下无发光/无光晕/点阵减淡；标题色、数值字体、卡片圆角跟主题 |
| `frontend-v2/src/pages/Dashboard.jsx` | 表头内联背景 `#0b0c12 → var(--panel)`；9 处写死色改 CSS 变量 |
| `frontend-v2/src/App.jsx` | 5 处写死色改 CSS 变量（带 fallback） |
| `frontend-v2/src/pages/ConfigPage.jsx` | 1 处写死青色改变量 |

**未改动**：`backend/` 全部、`strategies.yaml`、数据库、`exec/`、`script/baw/`。

---

## 四、回滚办法

```bash
cd E:/quant-bot
git log --oneline -3          # 找到本提交
git revert <commit>           # 或 git reset --hard 924602b
cd frontend-v2 && npm run build
```

服务重启：

```powershell
Get-NetTCPConnection -LocalPort 8899 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Sleep 3
Start-Process wscript.exe -ArgumentList 'E:\quant-bot\run_hidden.vbs'
Start-Sleep 16
```
