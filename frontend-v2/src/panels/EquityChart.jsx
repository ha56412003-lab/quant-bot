import { useEffect, useMemo, useState } from 'react'
import { Card, Segmented } from 'antd'
import Chart from '../Chart'
import { useI18n } from '../i18n'

// 标题视觉跟 Dashboard 右侧两张卡的 Card head 对齐（11.5px / 700 / 青色 / 字距 0.06em）。
// 从外层条搬到 Card head 里，看起来还是同一个标题，只是不再多占一层容器
const CARD_TITLE = {
  fontSize: 11.5, fontWeight: 700, color: 'var(--cyan, #45e6f5)', letterSpacing: '0.06em',
}

// 按主题取色。纸面主题卡片底是 #f6f5f1，亮荧光色在上面会发飘，整体压深一档。
// 全部主题：无外发光 —— 实体细线 + 纯色点阵，halo/blur/shadow 一律不用。
function palette(theme) {
  if (theme === 'paper') {
    return { line: '#c22b3a', glow: 'rgba(194,43,58,0)', dot: '#c22b3a',
             bg: '#f6f5f1', label: '#8a8880', tipBg: 'rgba(251,250,247,.97)',
             tipBorder: 'rgba(184,48,60,.35)', tipText: '#2a2a26',
             blur: 0, halo: 0, width: 2.3, dotR: 3.8, dotSize: 10 }
  }
  if (theme === 'mac-dark') {
    return { line: '#e58bb4', glow: 'rgba(229,139,180,0)', dot: '#e58bb4',
             bg: '#17161b', label: '#8b8496', tipBg: 'rgba(35,33,41,.98)',
             tipBorder: '#322f3c', tipText: '#f5f3f7',
             blur: 0, halo: 0, width: 2.1, dotR: 3.8, dotSize: 10 }
  }
  if (theme === 'mac-light') {
    return { line: '#b71c5a', glow: 'rgba(183,28,90,0)', dot: '#b71c5a',
             bg: '#ffffff', label: '#726a7a', tipBg: 'rgba(255,255,255,.99)',
             tipBorder: '#e6dae6', tipText: '#1c1b1f',
             blur: 0, halo: 0, width: 2.1, dotR: 3.8, dotSize: 10 }
  }
  // 注意：canvas 不解析 CSS 变量，line 必须写死色值（曾用 var(--cyan) 导致曲线变黑）
  return { line: '#22c9e8', glow: 'rgba(69,230,245,0)', dot: '#22c9e8',
           bg: '#0b0c12', label: 'rgba(150,160,180,.75)', tipBg: 'rgba(8,10,16,.94)',
           tipBorder: 'rgba(69,230,245,.35)', tipText: '#d8dde9',
           blur: 0, halo: 0, width: 2.3, dotR: 3.8, dotSize: 10 }
}

// 半调点阵 tile —— 每列从上到下圆点逐渐变小（顶部实、底部疏淡），纯色不透明
// 高 = size * rows，配合 repeat-x 使用：水平平铺、垂直拉伸到整个面积
function dotTile(color, size = 8, radius = 2.3, rows = 56) {
  const rBot = 1.6                       // 最底部半径：缩得缓和些，底部仍是实点
  const c = document.createElement('canvas')
  c.width = size; c.height = size * rows
  const g = c.getContext('2d')
  g.fillStyle = color
  for (let i = 0; i < rows; i++) {
    const t = rows > 1 ? i / (rows - 1) : 0
    const r = radius + (rBot - radius) * t
    if (r < 0.2) continue
    g.beginPath()
    g.arc(size / 2, size * i + size / 2, r, 0, Math.PI * 2)
    g.fill()
  }
  return c
}

// hex + alpha → rgba()，给遮罩层用
function hexA(hex, a) {
  const h = String(hex).replace('#', '')
  const n = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const r = parseInt(n.slice(0, 2), 16)
  const g = parseInt(n.slice(2, 4), 16)
  const b = parseInt(n.slice(4, 6), 16)
  return `rgba(${r},${g},${b},${a})`
}

// Paper and live equity are different universes ($10k virtual vs real wallet):
// the curve is stored per mode and NEVER mixed; default view = active mode.
export default function EquityChart({ activeMode, upnl = 0, realized = 0 }) {
  const { t, lang } = useI18n()
  const [view, setView] = useState(null)          // null until activeMode known
  const [series, setSeries] = useState([])
  const mode = view || activeMode || 'paper'

  // 主题跟着 <html data-theme> 走；每次渲染重读，切主题即时换色
  const theme = (typeof document !== 'undefined'
    && document.documentElement.getAttribute('data-theme')) || 'dark'
  const isPaper = theme === 'paper'
  const isMac = theme.startsWith('mac')

  useEffect(() => {
    let alive = true
    const load = () => fetch(`/api/equity?mode=${mode}`).then((r) => r.json())
      .then((d) => alive && setSeries(d.series || [])).catch(() => {})
    load()
    const timer = setInterval(load, 30000)
    return () => { alive = false; clearInterval(timer) }
  }, [mode])

  const titleStyle = isMac
    ? { fontSize: 12, fontWeight: 600, color: theme === 'mac-light' ? '#a81850' : '#e58bb4', letterSpacing: 0 }
    : CARD_TITLE

  const last = series.length ? Number(series[series.length - 1].equity) : null
  const P0 = palette(theme)

  const option = useMemo(() => {
    const P = palette(theme)
    const ts = series.map((r) => new Date(r.ts * 1000).toLocaleString(
      lang === 'en' ? 'en-US' : 'zh-CN',
      { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))
    const eq = series.map((r) => Number(r.equity.toFixed(2)))
    const tile = dotTile(P.dot, P.dotSize, P.dotR)

    return {
      // 贴边：参考图没有留白，线和点阵一直顶到画布边缘
      grid: { left: 6, right: 6, top: 10, bottom: 6, containLabel: false },
      tooltip: {
        trigger: 'axis',
        backgroundColor: P.tipBg,
        borderColor: P.tipBorder,
        borderWidth: 1,
        textStyle: { color: P.tipText, fontSize: 11 },
        axisPointer: { type: 'line', lineStyle: { color: P.dot, width: 1, type: 'dashed' } },
      },
      xAxis: { type: 'category', data: ts, boundaryGap: false, show: false },
      yAxis: { type: 'value', scale: true, show: false },
      series: [
        // ① 底层：线下的实体点阵 —— 纯色、不透明、无发光、无渐变压制
        {
          name: t('dash.equityName'), type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, lineStyle: { width: 0, opacity: 0 }, z: 1,
          areaStyle: { color: { image: tile, repeat: 'repeat-x' }, opacity: 1 },
          silent: true,
        },
        // ② 折线：实体细线，无外发光
        {
          name: t('dash.equityName'), type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, z: 5,
          lineStyle: {
            color: P.line, width: P.width,
          },
          emphasis: { lineStyle: { width: 3.2 } },
          tooltip: {
            valueFormatter: (v) => Number(v).toLocaleString('en-US',
              { maximumFractionDigits: 2 }) + ' U',
          },
        },
      ],
    }
  }, [series, lang, t, theme])

  return (
    <Card size="small"
          title={<span style={titleStyle}>{t('dash.equityCurve')}</span>}
          extra={(
            <Segmented size="small" value={mode} onChange={setView}
                       options={[{ label: t('hdr.paper'), value: 'paper' },
                                 { label: t('hdr.live'), value: 'live' }]} />
          )}
          style={{
            height: '100%', display: 'flex', flexDirection: 'column',
            background: 'var(--panel)', border: '1px solid var(--line)',
            borderRadius: isMac ? 12 : 5, overflow: 'hidden',
          }}
          styles={{
            header: {
              minHeight: 0, padding: '6px 12px',
              borderBottom: '1px solid var(--line)',
              background: 'transparent',
            },
            body: { flex: 1, minHeight: 0, padding: 0, position: 'relative' },
          }}>
      {/* 半透明浮框：净值 / 浮动盈亏 / 今日已实现 竖排
          浩哥 2026-11-07 定：曲线卡里原来只压了个净值小字，三项数字没地方看；
          改成毛玻璃浮框，压在左上角，不占画布。浮盈字号最大 —— 当下最关心这个。
          配色：净值苹果绿 / 浮盈玫红 / 今日已实现果冻橙 */}
      <div className="eq-float">
        <div className="eq-row">
          <span className="eq-lb">净值</span>
          <b className="eq-v eq-g">
            {last !== null ? last.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '--'}
            <i>U</i>
          </b>
        </div>
        <div className="eq-row">
          <span className="eq-lb">浮动盈亏</span>
          <b className="eq-v eq-r eq-big">
            {(upnl >= 0 ? '+' : '') + Number(upnl || 0).toFixed(2)}<i>U</i>
          </b>
        </div>
        <div className="eq-row">
          <span className="eq-lb">今日已实现</span>
          <b className="eq-v eq-o">
            {(realized >= 0 ? '+' : '') + Number(realized || 0).toFixed(2)}<i>U</i>
          </b>
        </div>
      </div>
      <Chart option={option} height="100%" />
    </Card>
  )
}
