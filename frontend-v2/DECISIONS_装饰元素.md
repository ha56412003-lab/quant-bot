# V2 账户面板装饰元素 · 最终参数锁定

> 2026-11-04 定稿，浩哥验收「这把完美了」。
> 两个元素都是从参考站 `F:\agent-display\index.html` 的 canvas 绘制源码逐像素逆向复刻，**不是自绘**。

## 一、布局总览

```
[雷达 INTAKE RADAR]   [账户净值 10199.62 U]   [总调度台 CHIEF]
   41 → 165              222 → 482              517 → 816
   124 × 142             260 × 62               299 × 151
```

净值块 `.sbig.equity` 范围：x 35 → 676，y 849 → 1016（641 × 167）

## 二、总调度台 CHIEF

**文件**：`frontend-v2/src/panels/ChiefDesk.jsx`
**CSS**：`frontend-v2/src/v2.css` → `.chief-wrap`

| 参数 | 值 |
|---|---|
| 显示尺寸 | **299 × 151**（`<ChiefDesk width={299} />`） |
| 定位 | `position:absolute; right:-140px; bottom:-6px; top:auto; transform:none` |
| 实测位置 | x **517 → 816**，y **871 → 1022** |
| 距净值数字 | 35px |
| 距浮动盈亏文字 | 65px |
| 层级 | `z-index:2; pointer-events:none` |

**原站绘制坐标**（canvas 原点偏移 OX=1078, OY=172，画布 238×120）：
- CHIEF 位置 `(1196, 204)`，半径 **r=13**，颜色 `#f3f4f8`，`shape='robot'`
- 耳机头带 `arc(0,-1,r+2.5, PI*1.08, PI*1.92)`，lineWidth 3，`#1a1c25`
- 左右耳罩 `rr(-r-5,-5,6,11,2)` / `rr(r-1,-5,6,11,2)`，`#1a1c25`
- 耳罩灯 `arc(±(r+2), 0, 1.6)`，`#ff3f8e`
- 眼睛 `ex=r*0.38, ey=-r*0.12, er=r*0.3`，`ellipse(±ex, ey, er*0.55, er*0.95)`，`#111318`
- 显示器 `rr(1104,196,46,28,2)`，屏 `(1107,199,40,22)` `#05070c`
- 桌面梯形 `(1092,226)(1300,226)(1308,240)(1084,240)` `#2a2e3b`
- 前板 `(1084,240,224,24)` `#181b25`
- 蜘蛛徽标 `drawMark(1196, 252, 16, '#d9dce6')`
- 黄字 `HEAD OF THE FLOOR · 总调度台`，`700 7.5px`，`#ffd84a`，`(1196, 276)` 居中

**动效**：
- 眨眼：每 2~5 秒一次，眼睛压到 15% 高，持续 0.12s
- 呼吸浮动：`sin(t*2) * 0.8px`
- 视线：`sin(t*0.7) * 0.8`（左右缓慢扫）
- 显示器扫描线：`(t*14) % 22` 青色横线

## 三、进料雷达 INTAKE RADAR

**文件**：`frontend-v2/src/panels/IntakeRadar.jsx`
**CSS**：`frontend-v2/src/v2.css` → `.radar-wrap`

| 参数 | 值 |
|---|---|
| 显示尺寸 | **124 × 142**（`<IntakeRadar width={124} />`） |
| 定位 | `position:absolute; left:6px; top:50%; transform:translateY(-50%)` |
| 实测位置 | x **41 → 165**，y **862 → 1004** |
| 距净值数字 | 57px |
| 层级 | `z-index:2; pointer-events:none` |

**原站绘制坐标**（OX=1092, OY=418，画布 124×142）：
- 底座梯形 `(1116,516)(1184,516)(1190,524)(1110,524)` `#20232e`
- 底座箱体 `(1110,524,80,28)` `#15171f`，描边 `#2a2e3b`
- 立杆 `(1150,516) → (1150,494)`，lineWidth 3，`#6b7184`
- 碟面 `translate(1150,482) rotate(-0.55) ellipse(0,0,28,19)` `#cfd3dc`
- 同心环 3 层 `ellipse(28k/4, 19k/4)` `#8b91a3`
- 十字准线 + 馈源臂 `(10,-24)` 圆点 r=3
- 铭牌 `(1116,532,68,12)` + `INTAKE RADAR` `800 7px`

**动效**（信号弧）：
```js
圆心 (1150, 482)，半径 20 + p*15（p 从 0 到 2.4）
角度 -1.15 ~ -0.35（朝右上）
lineWidth 2.4，颜色 rgba(76,240,176,alpha)，alpha 从 1 线性衰减
3 道弧，相位错开 0.8
```

## 四、踩过的坑（别重犯）

### 1. `.stats div` 有全局 `display:grid`
任何塞进 `.stats` 里的 div 都会被压扁（实测 12×4 像素）。必须逐项 `!important` 覆盖：
```css
display:block !important;
width:Npx !important;
height:Npx !important;
```

### 2. canvas 只画一次 → 截图/合成层里是空白
`getImageData` 能读到内容，但画面不显示。**必须用 `requestAnimationFrame` 持续重绘**。

### 3. 信号弧画到画布外
最初弧圆心 `(1158,462)` 正好在画布顶边上，半径 9~35 的弧全跑到画布上方被裁掉，导致「动效看不见，但代码在跑」。
**验证方法**：抓 20 帧统计绿色像素数，看是否波动（改前恒为 0，改后 250~523 波动）。

### 4. 用 `right` 定位时放大 = 往左长
宽度从 216 → 260 → 299，因为右边钉死，全部往左扩。右移 10px 会被左扩 29px 吃掉，**视觉上像往左移**。
**规则**：调"往右"必须同时看左右边缘，右移量要大于左扩量。

### 5. 别自己加"不能压数字"的隐形约束
浩哥要移就移，悬浮层压在什么上面都不影响功能。

## 五、修改方式

```bash
# 改尺寸：Dashboard.jsx 里的 width 属性 + v2.css 里的 width/height 两处都要改
cd E:/quant-bot/frontend-v2
npm run build          # 输出到 dist-v2
# 服务已在 8899 跑，刷新 http://127.0.0.1:8899/v2 即可
```
