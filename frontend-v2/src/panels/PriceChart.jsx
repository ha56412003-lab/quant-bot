import { useEffect, useMemo, useState } from 'react'
import { Card, Select, Space, Modal, Button, Tooltip } from 'antd'
import { ExpandAltOutlined } from '@ant-design/icons'
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
  // 卡片只有 271px 宽，画 200 多根 K 线 + 3 条线太挤 —— 点开看大图
  const [zoom, setZoom] = useState(false)

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
    const minTs = ks.length ? ks[0].ts : 0
    const marks = (data.marks || []).filter((m) => m.ts >= minTs).map((m) => {
      const idx = ks.findIndex((k, i) => k.ts <= m.ts && (i === ks.length - 1 || ks[i + 1].ts > m.ts))
      const isOpen = m.kind === 'open'
      const isBuy = m.side === 'long' || m.side === 'buy'
      return {
        coord: [idx >= 0 ? idx : cat.length - 1, m.price],
        value: isOpen ? t('dash.openMark') : t('dash.closeMark'),
        itemStyle: { color: isBuy ? '#ff5a6e' : '#3fd18b' },
        symbol: isOpen ? 'triangle' : 'circle',
        symbolRotate: isOpen && !isBuy ? 180 : 0,
        symbolSize: 11,
      }
    })
    // ── 图面改成「策略视角」（2026-11-12 夜）─────────────────────────
    // 原来：K 线 + 成交量副图。成交量对这套策略没有任何判据作用
    //       （六道闸里没有一条看量），白占 14% 高度。
    // 现在：K 线 + ZLSMA50（方向线）+ Chandelier Exit 上下两条止损通道线。
    //       这三条线全部来自后端 strategies._ce_zlsma，跟引擎判定同源。
    const ind = data.ind || {}
    const line = (arr, i) => (arr && arr[i] !== null && arr[i] !== undefined ? arr[i] : null)
    const zlsma = ks.map((_, i) => line(ind.zlsma, i))
    const ceLong = ks.map((_, i) => line(ind.ce_long, i))
    const ceShort = ks.map((_, i) => line(ind.ce_short, i))
    const hasInd = zlsma.some((v) => v !== null)

    return {
      grid: [{ left: 70, right: 16, top: 28, bottom: 30 }],
      legend: hasInd ? {
        top: 2, left: 70, itemWidth: 14, itemHeight: 8, itemGap: 14,
        textStyle: { fontSize: 10, color: '#7d8b9c' },
        data: ['ZLSMA50', t('dash.ceLong'), t('dash.ceShort')],
      } : undefined,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        // 悬停时把三条线的数值一起列出来 —— 看图就能对上「引擎此刻在哪个方向、
        // 止损离现价多远」，不用再切页面去查
        formatter: (ps) => {
          if (!ps || !ps.length) return ''
          const i = ps[0].dataIndex
          const k = ks[i]
          if (!k) return ''
          const px = (v) => (v === null || v === undefined ? '--' : Number(v).toFixed(4))
          const d = ind.dir && ind.dir[i]
          const dirTxt = d === 1 ? '多' : d === -1 ? '空' : '--'
          const rows = [
            `<b>${cat[i]}</b>`,
            `开 ${px(k.open)}　高 ${px(k.high)}`,
            `低 ${px(k.low)}　收 ${px(k.close)}`,
            `<span style="color:#45e6f5">ZLSMA50 ${px(zlsma[i])}</span>`,
            `<span style="color:#ff5a6e">${t('dash.ceShort')} ${px(ceShort[i])}</span>`,
            `<span style="color:#3fd18b">${t('dash.ceLong')} ${px(ceLong[i])}</span>`,
            `方向 ${dirTxt}`,
          ]
          return rows.join('<br/>')
        },
      },
      xAxis: [{ type: 'category', data: cat, gridIndex: 0, boundaryGap: true,
                axisLabel: { fontSize: 10 } }],
      yAxis: [{ scale: true, gridIndex: 0, splitLine: { lineStyle: { color: '#1d2633' } },
                axisLabel: { fontSize: 10 } }],
      series: [
        { name: symbol, type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: '#ff5a6e', color0: '#3fd18b',
                       borderColor: '#ff5a6e', borderColor0: '#3fd18b' },
          markPoint: { data: marks, label: { fontSize: 9, color: '#fff' } } },
        // ① 方向线：多空分界。策略的「时段确认」判的就是价格站在这条线的哪边
        { name: 'ZLSMA50', type: 'line', data: zlsma, xAxisIndex: 0, yAxisIndex: 0,
          showSymbol: false, smooth: false, connectNulls: false,
          lineStyle: { width: 1.8, color: '#45e6f5' }, itemStyle: { color: '#45e6f5' },
          z: 5 },
        // ② 空头止损通道（画在上方）：空单被这条线往上打穿就是止损
        { name: t('dash.ceShort'), type: 'line', data: ceShort, xAxisIndex: 0, yAxisIndex: 0,
          showSymbol: false, connectNulls: false,
          lineStyle: { width: 1.1, color: 'rgba(255,90,110,0.72)', type: 'dashed' },
          itemStyle: { color: '#ff5a6e' }, z: 4 },
        // ③ 多头止损通道（画在下方）
        { name: t('dash.ceLong'), type: 'line', data: ceLong, xAxisIndex: 0, yAxisIndex: 0,
          showSymbol: false, connectNulls: false,
          lineStyle: { width: 1.1, color: 'rgba(63,209,139,0.72)', type: 'dashed' },
          itemStyle: { color: '#3fd18b' }, z: 4 },
      ],
      // 起点 33%：ZLSMA50 需要 98 根预热，300 根里第 98 根是 32.7% ——
      // 从这里往右每一根都有线，左边那段（缺指标）不显示，既没有断头，
      // 又比一味往右拉能看到更多历史和开平仓标记（62% 会把前天的标记藏掉）。
      dataZoom: [{ type: 'inside', xAxisIndex: [0], start: 33, end: 100 }],
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
        <Tooltip title={t('dash.zoomHint')}>
          <Button size="small" type="text" icon={<ExpandAltOutlined />}
                  onClick={() => setZoom(true)}
                  style={{ padding: '0 4px', height: 22, color: 'var(--cyan, #45e6f5)' }} />
        </Tooltip>
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

      {/* 大图：同样的 option，屏幕宽了每根 K 线就宽，三条线自然分得开。
          滚轮/拖拽照样能缩放平移（dataZoom inside 在大图里更好用）。 */}
      <Modal
        open={zoom}
        onCancel={() => setZoom(false)}
        footer={null}
        width="94vw"
        style={{ top: 16, maxWidth: 1600 }}
        title={
          <span style={{ fontSize: 13, fontWeight: 700 }}>
            {t('dash.priceChart')} · {symbol} · {interval}
          </span>
        }
        styles={{ body: { padding: '4px 8px 8px' } }}>
        <div style={{ height: '74vh', minHeight: 420 }}>
          {option && <Chart option={option} height="100%" />}
        </div>
      </Modal>
    </Card>
  )
}
