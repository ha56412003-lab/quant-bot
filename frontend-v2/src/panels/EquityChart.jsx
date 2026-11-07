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
// MAC 两套走「无发光极简」：细实线 + 极淡点阵，光晕/blur 全部归零。
function palette(theme) {
  if (theme === 'paper') {
    return { line: '#c22b3a', glow: 'rgba(194,43,58,.95)', dot: 'rgba(194,43,58,.70)',
             bg: '#f6f5f1', label: '#8a8880', tipBg: 'rgba(251,250,247,.97)',
             tipBorder: 'rgba(184,48,60,.35)', tipText: '#2a2a26',
             blur: 17, halo: 0.16, width: 1.7, dotR: 1.0, dotSize: 6 }
  }
  if (theme === 'mac-dark') {
    return { line: '#0a84ff', glow: 'rgba(10,132,255,0)', dot: 'rgba(10,132,255,.26)',
             bg: '#1c1c1e', label: '#8e8e93', tipBg: 'rgba(44,44,46,.98)',
             tipBorder: 'rgba(255,255,255,.14)', tipText: '#f5f5f7',
             blur: 0, halo: 0, width: 1.4, dotR: 0.85, dotSize: 7 }
  }
  if (theme === 'mac-light') {
    return { line: '#007aff', glow: 'rgba(0,122,255,0)', dot: 'rgba(0,122,255,.16)',
             bg: '#ffffff', label: '#6e6e73', tipBg: 'rgba(255,255,255,.99)',
             tipBorder: 'rgba(0,0,0,.12)', tipText: '#1d1d1f',
             blur: 0, halo: 0, width: 1.4, dotR: 0.85, dotSize: 7 }
  }
  return { line: 'var(--cyan, #45e6f5)', glow: 'rgba(69,230,245,.90)', dot: 'rgba(69,230,245,.55)',
           bg: '#0b0c12', label: 'rgba(150,160,180,.75)', tipBg: 'rgba(8,10,16,.94)',
           tipBorder: 'rgba(69,230,245,.35)', tipText: '#d8dde9',
           blur: 15, halo: 0.16, width: 1.7, dotR: 1.0, dotSize: 6 }
}

// 半调点阵 tile —— 参考图是线下铺一层规则小圆点，不是实色渐变
function dotTile(color, size = 6, radius = 1.0) {
  const c = document.createElement('canvas')
  c.width = size; c.height = size
  const g = c.getContext('2d')
  g.fillStyle = color
  g.beginPath()
  g.arc(size / 2, size / 2, radius, 0, Math.PI * 2)
  g.fill()
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
export default function EquityChart({ activeMode }) {
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
    ? { fontSize: 12, fontWeight: 600, color: theme === 'mac-light' ? '#0062cc' : '#0a84ff', letterSpacing: 0 }
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
        // ① 底层：线下的半调点阵（对应参考图的粉色网点面）
        {
          name: t('dash.equityName'), type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, lineStyle: { width: 0, opacity: 0 }, z: 1,
          areaStyle: { color: { image: tile, repeat: 'repeat' }, opacity: 1 },
          silent: true,
        },
        // ② 遮罩：把点阵从上往下压暗，做出参考图那种「上浓下淡」
        //    颜色必须等于卡片底色，否则整块区域会被染色
        {
          name: '_veil', type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, lineStyle: { width: 0, opacity: 0 }, z: 2,
          areaStyle: {
            color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0,    color: 'rgba(0,0,0,0)' },
                { offset: 0.30, color: hexA(P.bg, 0.08) },
                { offset: 0.62, color: hexA(P.bg, 0.55) },
                { offset: 1,    color: hexA(P.bg, 0.97) },
              ] },
          },
          silent: true,
        },
        // ③ 弥散光晕层：粗而极淡的一条，把 shadow 的硬边撑开
        {
          name: '_halo', type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, z: 4, silent: true,
          lineStyle: {
            color: P.line, width: 5, opacity: P.halo,
            shadowColor: P.glow, shadowBlur: P.blur > 0 ? 24 : 0, shadowOffsetY: 0,
          },
        },
        // ④ 折线：细芯 + 外发光 = 参考图那种霓虹感
        {
          name: t('dash.equityName'), type: 'line', data: eq, showSymbol: false,
          smooth: 0.15, z: 5,
          lineStyle: {
            color: P.line, width: P.width,
            shadowColor: P.glow, shadowBlur: P.blur, shadowOffsetY: 0,
          },
          emphasis: { lineStyle: { width: 2.2 } },
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
      {/* 左上角数值：参考图那个浅灰数字，压在画布上而不是占一行 */}
      {last !== null && (
        <div style={{
          position: 'absolute', top: 8, left: 10, zIndex: 3, pointerEvents: 'none',
          fontFamily: isMac
            ? '-apple-system, BlinkMacSystemFont, SF Pro Text, PingFang SC, sans-serif'
            : 'Cascadia Mono, Consolas, monospace',
          fontSize: isPaper ? 15 : (isMac ? 17 : 14),
          fontWeight: isMac ? 600 : 500,
          fontVariantNumeric: 'tabular-nums',
          color: P0.label, letterSpacing: '0.01em',
        }}>
          {last.toLocaleString('en-US', { maximumFractionDigits: 1 })} U
        </div>
      )}
      <Chart option={option} height="100%" />
    </Card>
  )
}
