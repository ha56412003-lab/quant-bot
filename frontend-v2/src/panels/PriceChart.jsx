import { useEffect, useMemo, useState } from 'react'
import { Card, Select, Space } from 'antd'
import Chart from '../Chart'
import { api } from '../api'
import { useI18n } from '../i18n'

const INTERVALS = ['5m', '15m', '1h', '4h']

// 固定主流 10 币 —— 不管监控池怎么换，这一组永远在（顺序按市值排）
const MAJORS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
                'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'TRXUSDT']

// 标题视觉跟 EquityChart 的 CARD_TITLE 一致（11.5px / 700 / 青色 / 字距 0.06em）：
// 两张右侧卡的标题都在 Card head 里，视觉必须统一
const CARD_TITLE = {
  fontSize: 11.5, fontWeight: 700, color: 'var(--cyan, #45e6f5)', letterSpacing: '0.06em',
}

export default function PriceChart({ universe }) {
  const { t, lang } = useI18n()
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [interval, setItv] = useState('15m')
  const [data, setData] = useState(null)
  const [heldSyms, setHeldSyms] = useState([])
  const [poolSyms, setPoolSyms] = useState([])

  // 持仓列表：开平仓后下拉要能立刻反映（15s 一次）
  useEffect(() => {
    let alive = true
    const load = () => api.positions()
      .then((d) => { if (alive) setHeldSyms((d.positions || []).map((p) => p.symbol)) })
      .catch(() => {})
    load()
    const timer = setInterval(load, 15000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  // 监控池（三层池里的 monitor，目前 55 个）：池子 5 分钟才重建一次，
  // 所以 2 分钟拉一次就够 —— /api/pool 一次约 100KB，别按 15s 拉，白费流量
  useEffect(() => {
    let alive = true
    const load = () => api.pool()
      .then((d) => { if (alive) setPoolSyms(d.monitor || []) })
      .catch(() => {})
    load()
    const timer = setInterval(load, 120000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  useEffect(() => {
    let alive = true
    const load = () => api.klines(symbol, interval).then((d) => alive && setData(d)).catch(() => {})
    load()
    const timer = setInterval(load, 60000)
    return () => { alive = false; clearInterval(timer) }
  }, [symbol, interval])

  // 下拉分组：持仓置顶 → 主流 10 个 → 监控池
  // 同一个币只出现一次，优先级高的组先占（持仓 > 主流 > 监控池）
  // 监控池取 /api/pool 的 monitor（≈55 个）；universe 是全量候选池（200 个），太长了不用
  const symGroups = useMemo(() => {
    const held = heldSyms
    const pool = poolSyms.length ? poolSyms : (universe || [])
    const majorRest = MAJORS.filter((s2) => !held.includes(s2))
    const poolRest = pool.filter((s2) => !held.includes(s2) && !MAJORS.includes(s2))
    const mk = (arr) => arr.map((s2) => ({ value: s2, label: s2 }))
    const groups = []
    if (held.length) {
      groups.push({ label: `持仓 · ${held.length}`, options: mk(held) })
    }
    groups.push({ label: '主流 · 10', options: mk(majorRest) })
    if (poolRest.length) {
      groups.push({ label: `监控池 · ${poolRest.length}`, options: mk(poolRest) })
    }
    return groups
  }, [heldSyms, poolSyms, universe])

  const option = useMemo(() => {
    if (!data) return null
    const ks = data.klines || []
    const cat = ks.map((k) => new Date(k.ts * 1000).toLocaleString(
      lang === 'en' ? 'en-US' : 'zh-CN',
      { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))
    const ohlc = ks.map((k) => [k.open, k.close, k.low, k.high])
    const vols = ks.map((k) => k.volume)
    const minTs = ks.length ? ks[0].ts : 0
    const marks = (data.marks || []).filter((m) => m.ts >= minTs).map((m) => {
      const idx = ks.findIndex((k, i) => k.ts <= m.ts && (i === ks.length - 1 || ks[i + 1].ts > m.ts))
      const isOpen = m.kind === 'open'
      const isBuy = m.side === 'long' || m.side === 'buy'
      return {
        coord: [idx >= 0 ? idx : cat.length - 1, m.price],
        value: isOpen ? t('dash.openMark') : t('dash.closeMark'),
        itemStyle: { color: isBuy ? 'var(--red, #d4595e)' : 'var(--green, #3fa86a)' },
        symbol: isOpen ? 'triangle' : 'circle',
        symbolRotate: isOpen && !isBuy ? 180 : 0,
        symbolSize: 11,
      }
    })
    return {
      grid: [{ left: 70, right: 16, top: 24, height: '62%' },
             { left: 70, right: 16, top: '78%', height: '14%' }],
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      xAxis: [{ type: 'category', data: cat, gridIndex: 0 },
              { type: 'category', data: cat, gridIndex: 1, axisLabel: { show: false } }],
      yAxis: [{ scale: true, gridIndex: 0, splitLine: { lineStyle: { color: '#1d2633' } } },
              { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }],
      series: [
        { name: symbol, type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: 'var(--red, #d4595e)', color0: 'var(--green, #3fa86a)',
                       borderColor: 'var(--red, #d4595e)', borderColor0: 'var(--green, #3fa86a)' },
          markPoint: { data: marks, label: { fontSize: 9, color: '#fff' } } },
        { name: t('dash.volume'), type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1,
          itemStyle: {
            // 成交量跟随该根 K 线涨跌着色（红涨绿跌）
            color: (params) => {
              const k = ks[params.dataIndex]
              if (!k) return '#3a4553'
              return k.close >= k.open
                ? 'rgba(212, 89, 94, 0.8)'
                : 'rgba(63, 168, 106, 0.8)'
            },
          } },
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 55, end: 100 }],
    }
  }, [data, symbol, lang, t])

  return (
    <Card size="small"
          title={<span style={CARD_TITLE}>{t('dash.priceChart')}</span>}
          extra={
      <Space>
        <Select size="small" value={symbol} onChange={setSymbol} style={{ width: 118 }}
                options={symGroups} listHeight={400}
                popupMatchSelectWidth={false}
                // 持仓组用暖色区分，一眼看出哪几个是自己正在做的
                optionRender={(o) => {
                  const held = heldSyms.includes(o.value)
                  return (
                    <span style={{
                      color: held ? 'var(--joint, #d4595e)' : undefined,
                      fontWeight: held ? 700 : 400,
                    }}>
                      {held ? '● ' : ''}{o.label}
                    </span>
                  )
                }} />
        <Select size="small" value={interval} onChange={setItv} style={{ width: 70 }}
                options={INTERVALS.map((i) => ({ value: i, label: i }))} />
      </Space>
    }
          style={{
            height: '100%', display: 'flex', flexDirection: 'column',
            background: 'var(--panel)', border: '1px solid var(--line)',
            borderRadius: 5, overflow: 'hidden',
          }}
          styles={{
            header: {
              minHeight: 0, padding: '6px 12px',
              borderBottom: '1px solid var(--line)',
              background: 'transparent',
            },
            body: { flex: 1, minHeight: 0, padding: '6px 8px 8px' },
          }}>
      {option && <Chart option={option} height="100%" />}
    </Card>
  )
}
