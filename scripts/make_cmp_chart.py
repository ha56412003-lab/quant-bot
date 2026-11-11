# -*- coding: utf-8 -*-
"""生成本地 vs 服务器的对比图（SVG → 浏览器截图成 PNG）

配色遵循浩哥的「红涨绿跌」：本地（新版）用红 #c4182c，服务器用中性蓝灰 #6b8299。
只读 curve_cmp.json，输出 HTML 到 scratch，PNG 落到 docs/screenshots/。
"""
import datetime
import json
import os

SC = r"E:\Hermes\cache\scratch"
OUT_HTML = os.path.join(SC, "cmp_chart.html")

RED = "#c4182c"
GREY = "#4f6b80"
INK = "#1b1b1b"
MUT = "#6a6a6a"
LINE = "#ececec"

D = json.load(open(os.path.join(SC, "curve_cmp.json"), encoding="utf-8"))
L, S = D["local"], D["server"]

W, H = 1180, 582
# 曲线区
CX0, CX1, CY0, CY1 = 78, 700, 62, 470

allp = [p for v in (L["cum"], S["cum"]) for p in v]
tmin = min(p[0] for p in allp)
tmax = max(p[0] for p in allp)
import math
vmax = math.ceil(max(p[1] for p in allp) * 1.05 / 10.0) * 10
vmin = math.floor(min(0.0, min(p[1] for p in allp) * 1.15) / 10.0) * 10


def px(ts):
    return CX0 + (ts - tmin) / (tmax - tmin) * (CX1 - CX0)


def py(v):
    return CY1 - (v - vmin) / (vmax - vmin) * (CY1 - CY0)


def poly(cum):
    pts = [(px(tmin), py(0.0))] + [(px(ts), py(v)) for ts, v in cum]
    return " ".join("%.1f,%.1f" % p for p in pts)


# ── Y 轴刻度
ysteps = []
step = 10 if vmax > 40 else (5 if vmax > 15 else 2)
v = int(vmin)
while v <= vmax + 0.01:
    ysteps.append(v)
    v += step

# ── X 轴刻度（每 10 天）
xticks = []
t = datetime.datetime.fromtimestamp(tmin, datetime.timezone(datetime.timedelta(hours=8)))
t = datetime.datetime(t.year, t.month, t.day, tzinfo=t.tzinfo)
t0 = t
while t.timestamp() <= tmax:
    if (t - t0).days % 10 == 0:
        xticks.append((t.timestamp(), "%d/%d" % (t.month, t.day)))
    t += datetime.timedelta(days=1)

_dt = datetime.datetime.fromtimestamp(tmax, datetime.timezone(datetime.timedelta(hours=8)))
if xticks and (tmax - xticks[-1][0]) < 5 * 86400:
    xticks[-1] = (tmax, "%d/%d" % (_dt.month, _dt.day))
else:
    xticks.append((tmax, "%d/%d" % (_dt.month, _dt.day)))

g = []
for yv in ysteps:
    y = py(yv)
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"/>'
             % (CX0, y, CX1, y, LINE))
    g.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" text-anchor="end">%d R</text>'
             % (CX0 - 10, y + 4, MUT, yv))
for ts, lab in xticks:
    x = px(ts)
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1"/>'
             % (x, CY0, x, CY1, LINE))
    g.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" text-anchor="middle">%s</text>'
             % (x, CY1 + 20, MUT, lab))

# 零线加粗
g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#c9c9c9" stroke-width="1.4"/>'
         % (CX0, py(0), CX1, py(0)))

# 本地线 + 端点
g.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.4" '
         'stroke-linejoin="round" stroke-linecap="round"/>' % (poly(L["cum"]), RED))
lt, lv = L["cum"][-1]
g.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="%s"/>' % (px(lt), py(lv), RED))
g.append('<text x="%.1f" y="%.1f" fill="%s" font-size="13" font-weight="700" '
         'text-anchor="end">+%.1fR</text>' % (px(lt) - 2, py(lv) - 12, RED, lv))

# 服务器线 + 端点
g.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.9" '
         'stroke-linejoin="round" stroke-linecap="round" opacity="0.92"/>'
         % (poly(S["cum"]), GREY))
st, sv = S["cum"][-1]
g.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/>' % (px(st), py(sv), GREY))
g.append('<text x="%.1f" y="%.1f" fill="%s" font-size="12" font-weight="600" '
         'text-anchor="end">+%.1fR</text>' % (px(st) - 2, py(sv) + 20, GREY, sv))

# ── 右侧指标对比
MX = 748
rows = [
    ("胜率", L["six"]["win"], S["six"]["win"], "%.1f%%", 100, "大更好"),
    ("盈亏比", L["six"]["pl"], S["six"]["pl"], "%.2f", 2.0, "大更好"),
    ("净值 (R)", L["six"]["net"], S["six"]["net"], "%.1f", 60.0, "大更好"),
    ("最大回撤 (R) · 越短越好", abs(L["six"]["mdd"]), abs(S["six"]["mdd"]), "%.1f", 15.0, "小更好"),
    ("单量 (单/天)", L["six"]["per_day"], S["six"]["per_day"], "%.2f", 6.0, "看场景"),
]
mcols = []
y = 148
for name, a, b, fmt, scale, _hint in rows:
    mcols.append('<text x="%d" y="%d" fill="%s" font-size="12.5">%s</text>' % (MX, y, INK, name))
    # 本地条
    wa = max(4, min(1.0, a / scale) * 300)
    mcols.append('<rect x="%d" y="%d" width="300" height="9" rx="4.5" fill="#f2f2f2"/>' % (MX, y + 10))
    mcols.append('<rect x="%d" y="%d" width="%.1f" height="9" rx="4.5" fill="%s"/>' % (MX, y + 10, wa, RED))
    mcols.append('<text x="%.1f" y="%d" fill="%s" font-size="11.5" font-weight="700">%s</text>'
                 % (MX + 308, y + 18, RED, fmt % a))
    # 服务器条
    wb = max(4, min(1.0, b / scale) * 300)
    mcols.append('<rect x="%d" y="%d" width="300" height="9" rx="4.5" fill="#f2f2f2"/>' % (MX, y + 33))
    mcols.append('<rect x="%d" y="%d" width="%.1f" height="9" rx="4.5" fill="%s"/>' % (MX, y + 33, wb, GREY))
    mcols.append('<text x="%.1f" y="%d" fill="%s" font-size="11.5" font-weight="700">%s</text>'
                 % (MX + 308, y + 41, GREY, fmt % b))
    y += 78

html = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{width:%(W)dpx;height:%(H)dpx;background:#fff;
       font-family:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;
       -webkit-font-smoothing:antialiased}
  svg{display:block}
</style></head><body>
<svg width="%(W)d" height="%(H)d" viewBox="0 0 %(W)d %(H)d" xmlns="http://www.w3.org/2000/svg">
  <text x="40" y="34" fill="%(INK)s" font-size="17" font-weight="700">本地新版 vs 服务器在跑的版本 · 40 币 · 9/4–11/11 累计净值</text>
  <text x="40" y="52" fill="%(MUT)s" font-size="11.5">同一份 K 线缓存、同一份母本代码，只换参数 · K 线已补到 2026-11-11 12:00</text>

  <rect x="760" y="76" width="10" height="10" rx="2" fill="%(RED)s"/>
  <text x="776" y="85" fill="%(INK)s" font-size="11.5">本地（ce_mult 1.5 · 限2笔 · 保本1.5R）</text>
  <rect x="760" y="96" width="10" height="10" rx="2" fill="%(GREY)s"/>
  <text x="776" y="105" fill="%(INK)s" font-size="11.5">服务器（ce_mult 2.0 · 16h强平 · 无保本）</text>

  %(GRID)s
  %(LINES)s
  %(MCOLS)s

  <text x="40" y="508" fill="#555" font-size="11.5">
    净值 R ＝ 每笔盈亏 ÷ 该笔初始风险（1R）。两条线起点都是 0，终点即累计。
  </text>
  <text x="40" y="528" fill="#555" font-size="11.5">按本金 1 万 U、每笔风险 1.5%% 算：1R ≈ 150U　→　本地 +45.9R ≈ 6,889U（回撤 9.7R ≈ −1,455U）；服务器 +9.2R ≈ 1,380U（回撤 15.7R ≈ −2,348U）。</text>
  <text x="40" y="546" fill="#555" font-size="11.5">
    本地每笔 +0.163R，服务器每笔 +0.049R；本地胜率高 12.9 个点、最大回撤还小 5.95R。　本图样本已含 11-10 那波 V 型反转（6 个共线空单），是补齐数据后的真实口径。
  </text>
</svg>
</body></html>
""" % dict(W=W, H=H, INK=INK, MUT=MUT, RED=RED, GREY=GREY,
           GRID="\n  ".join(g), LINES="", MCOLS="\n  ".join(mcols))

# 曲线已经在 g 里，简化：全部塞进 GRID
html = html.replace("%(LINES)s", "")

with open(OUT_HTML, "w", encoding="utf-8") as f:
    f.write(html)
print("HTML:", OUT_HTML, os.path.getsize(OUT_HTML), "bytes")
