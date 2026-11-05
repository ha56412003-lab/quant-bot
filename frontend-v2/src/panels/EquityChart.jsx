import { useEffect, useMemo, useState } from 'react'
import { Card, Segmented } from 'antd'
import Chart from '../Chart'
import { useI18n } from '../i18n'

// Paper and live equity are different universes ($10k virtual vs real wallet):
// the curve is stored per mode and NEVER mixed; default view = active mode.
export default function EquityChart({ activeMode }) {
  const { t, lang } = useI18n()
  const [view, setView] = useState(null)          // null until activeMode known
  const [series, setSeries] = useState([])
  const mode = view || activeMode || 'paper'

  useEffect(() => {
    let alive = true
    const load = () => fetch(`/api/equity?mode=${mode}`).then((r) => r.json())
      .then((d) => alive && setSeries(d.series || [])).catch(() => {})
    load()
    const timer = setInterval(load, 30000)
    return () => { alive = false; clearInterval(timer) }
  }, [mode])

  const option = useMemo(() => {
    const ts = series.map((r) => new Date(r.ts * 1000).toLocaleString(
      lang === 'en' ? 'en-US' : 'zh-CN',
      { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))
    const eq = series.map((r) => Number(r.equity.toFixed(2)))
    return {
      grid: { left: 62, right: 20, top: 18, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(8,10,16,.94)',
        borderColor: 'rgba(69,230,245,.35)',
        textStyle: { color: '#d8dde9', fontSize: 11 },
      },
      xAxis: {
        type: 'category', data: ts, boundaryGap: false,
        axisLine: { lineStyle: { color: '#2a3140' } },
        axisTick: { show: false },
        axisLabel: {
          color: '#6b7183', fontSize: 9,
          fontFamily: 'Cascadia Mono, Consolas, monospace',
          hideOverlap: true, interval: 'auto', rotate: 0,
          formatter: (v) => String(v).split(' ').slice(-1)[0],   // 只留时间
        },
      },
      yAxis: {
        type: 'value', scale: true,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: {
          color: '#6b7183', fontSize: 9.5,
          fontFamily: 'Cascadia Mono, Consolas, monospace',
          formatter: (v) => v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
        },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,.05)', type: 'dashed' } },
      },
      series: [{
        name: t('dash.equityName'), type: 'line', data: eq, showSymbol: false, smooth: 0.35,
        lineStyle: {
          color: '#45e6f5', width: 2,
          shadowColor: 'rgba(69,230,245,.85)', shadowBlur: 10, shadowOffsetY: 2,
        },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(69,230,245,0.26)' },
                       { offset: 0.7, color: 'rgba(69,230,245,0.05)' },
                       { offset: 1, color: 'rgba(69,230,245,0)' }] } },
        markPoint: {
          symbol: 'circle', symbolSize: 7, data: [{ type: 'max', name: '最高' }],
          itemStyle: { color: '#45e6f5', borderColor: '#0b0c12', borderWidth: 2 },
          label: { show: false },
        },
      }],
    }
  }, [series, lang, t])
  return (
    <Card size="small" 
          extra={(
            <Segmented size="small" value={mode} onChange={setView}
                       options={[{ label: t('hdr.paper'), value: 'paper' },
                                 { label: t('hdr.live'), value: 'live' }]} />
          )}>
      <Chart option={option} height="100%" />
    </Card>
  )
}
