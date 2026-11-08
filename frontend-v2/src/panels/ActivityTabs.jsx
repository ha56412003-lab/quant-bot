import { useEffect, useRef, useState } from 'react'
import { Card, Table, Tabs, Tag, Typography } from 'antd'
import { fmtPnl } from '../api'
import { useI18n } from '../i18n'

// Clawby 的经济日历返回的全是英文长句（"Seasonally adjusted changes in
// non-agricultural employment…"），照原样显示等于没显示 —— 浩哥看不懂。
// 按关键词翻成中文，认不出来的就截断保留英文。
const EVENT_ZH = [
  [/non-?farm|non-?agricultural/i, '非农就业'],
  [/unemployment rate/i, '失业率'],
  [/average hour/i, '平均时薪'],
  [/employment population|employed population/i, '就业人口'],
  [/unemployment/i, '失业数据'],
  [/consumer confidence/i, '消费者信心'],
  [/\bCPI\b|consumer price/i, 'CPI 消费者物价'],
  [/\bPPI\b|producer price/i, 'PPI 生产者物价'],
  [/federal funds|\bFOMC\b|interest rate decision/i, '美联储利率决议'],
  [/\bGDP\b/i, 'GDP'],
  [/retail sales/i, '零售销售'],
  [/jobless claim/i, '初请失业金'],
  [/\bISM\b/i, 'ISM 制造业指数'],
  [/\bPMI\b/i, 'PMI 景气指数'],
  [/durable goods/i, '耐用品订单'],
  [/trade balance/i, '贸易收支'],
  [/building permit|housing start|new home/i, '房屋数据'],
  [/crude oil|petroleum|inventor/i, '原油库存'],
  [/personal (income|spending|consumption)/i, '个人收支'],
  [/michigan/i, '密歇根消费者信心'],
]

const evName = (title) => {
  if (!title) return '-'
  for (const [re, zh] of EVENT_ZH) if (re.test(title)) return zh
  return title.length > 34 ? title.slice(0, 34) + '…' : title
}

// 分钟 → 「2h38m」/「45m」
const fmtCd = (mins) => {
  if (mins == null) return '-'
  if (mins <= 0) return '0m'
  const h = Math.floor(mins / 60)
  return h > 0 ? `${h}h${String(mins % 60).padStart(2, '0')}m` : `${mins}m`
}

export default function ActivityTabs({ signals, trades, logs, gates, events }) {
  const { t, lang } = useI18n()

  // 表格高度按实际可用空间算（2026-11-06 浩哥：列表优先适配高度，不够再用滑块）。
  // 以前 5 个表都写死 scroll.y=330，容器 495px 也只用 330 —— 底下留一大片空白。
  // 现在量 .ant-tabs-content-holder 的实测高度，减掉状态行和表头，剩下的全给表格。
  // 数据多时超出这段高度才会出现滚动条，数据少时表格就短 —— 但面板本身始终是满高的。
  const wrapRef = useRef(null)
  const calcRef = useRef(() => {})
  const [scrollY, setScrollY] = useState(330)

  useEffect(() => {
    const host = wrapRef.current
    if (!host) return
    const calc = () => {
      const holder = host.querySelector('.ant-tabs-content-holder')
      const pane = host.querySelector('.ant-tabs-tabpane-active')
      const avail = (holder || pane)?.clientHeight || 0
      if (avail < 120) return                       // 还没布局好，等下一次
      const statusEl = pane?.querySelector('[data-act-status]')
      const statusH = statusEl ? statusEl.offsetHeight + 6 : 0
      const theadH = 34                             // size="small" 表头
      // 按整行取整：高度不是行高的整数倍时，最底下一行会露出半截
      // （实测信号流就有一行被切掉一半）。量第一行实际高度，能放几行就给几行。
      const rowEl = pane?.querySelector('.ant-table-tbody tr.ant-table-row')
      const rowH = rowEl ? Math.max(28, Math.round(rowEl.getBoundingClientRect().height)) : 39
      const usable = avail - statusH - theadH
      const nRows = Math.max(3, Math.floor(usable / rowH))
      // 行高平摊：整行取整会剩 37–46px 空白（不够一行），
      // 把余量摊到每行，N 行正好等于可用高度 —— 既没有半截行，也不留空白。
      const finalRowH = Math.floor(usable / nRows)
      const next = nRows * finalRowH
      // 差 4px 以内不更新：否则 ResizeObserver → setState → 重排 → 再触发，来回抖
      setScrollY((prev) => (Math.abs(prev - next) < 4 ? prev : next))
      // antd 的 scroll.y 是 max-height —— 数据少时表格只有内容那么高，
      // 面板下方就留一大片空白（经济事件只有 3 条时实测空了 391px）。
      // 通过 CSS 变量下发 min-height（直接改 el.style 会被 React 重渲染冲掉）：
      // 数据少 → 表格拉满（空白留在表格内），数据多 → 超出部分出滚动条。
      host.style.setProperty('--act-scroll-y', `${next}px`)
      host.style.setProperty('--act-row-h', `${finalRowH}px`)
    }
    calcRef.current = calc
    calc()
    const ro = new ResizeObserver(calc)
    ro.observe(host)
    // 字体/窗口变化时也重算一次
    window.addEventListener('resize', calc)
    return () => { ro.disconnect(); window.removeEventListener('resize', calc) }
  }, [])

  // 切换标签后各 tab 的状态行高度不同（经济事件/三道光闸有状态行），要重算
  const onTabChange = () => setTimeout(() => calcRef.current(), 80)


  const ts = (v) => new Date(v * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  const signalCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('dash.col.strategy'), dataIndex: 'strategy', width: 60, render: (v) => v.split('_')[0] },
    { title: t('dash.col.symbol'), dataIndex: 'symbol', width: 80 },
    { title: t('dash.col.side'), dataIndex: 'side', width: 50,
      render: (v) => <Tag color={v === 'long' ? 'red' : 'green'}>{v === 'long' ? t('dash.long') : t('dash.short')}</Tag> },
    { title: t('act.result'), key: 'acted', width: 110,
      render: (_, r) => {
        if (!r.acted) return <Tag color="default">{r.skip_reason || t('act.skipped')}</Tag>
        // acted=1 只说明「当时开了仓」，仓位现在还在不在要看 pos_status
        if (r.pos_status === 'open') return <Tag color="green">{t('act.holding')}</Tag>
        if (r.pos_status === 'closed') {
          const p = r.pos_pnl
          return <Tag color="default">{t('act.closed')}{p == null ? '' : ' '}
            {p != null && <span style={{ color: p >= 0 ? 'var(--red, #d4595e)' : 'var(--green, #3fa86a)' }}>{fmtPnl(p)}</span>}
          </Tag>
        }
        return <Tag color="blue">{t('act.executed')}</Tag>
      } },
    { title: t('act.reason'), dataIndex: 'reason', ellipsis: true },
  ]
  const tradeCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('dash.col.strategy'), dataIndex: 'strategy', width: 60, render: (v) => v.split('_')[0] },
    { title: t('dash.col.symbol'), dataIndex: 'symbol', width: 80 },
    { title: t('act.kind'), dataIndex: 'kind', width: 55,
      render: (v) => <Tag color={v === 'open' ? 'blue' : 'purple'}>{v === 'open' ? t('dash.openMark') : t('dash.closeMark')}</Tag> },
    { title: t('act.price'), dataIndex: 'price', width: 90, render: (v) => v?.toPrecision(6) },
    { title: t('dash.col.qty'), dataIndex: 'qty', width: 80, render: (v) => v?.toPrecision(4) },
    { title: t('journal.col.pnl'), dataIndex: 'pnl', width: 80,
      render: (v) => v == null ? '-' :
        <span style={{ color: v >= 0 ? 'var(--red, #d4595e)' : 'var(--green, #3fa86a)' }}>{fmtPnl(v)}</span> },
    { title: t('journal.col.mode'), dataIndex: 'mode', width: 60,
      render: (v) => <Tag color={v === 'live' ? 'red' : 'blue'} style={{ fontSize: 10 }}>{v}</Tag> },
  ]
  const logCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('act.level'), dataIndex: 'level', width: 55,
      render: (v) => <Tag color={{ error: 'red', warn: 'orange' }[v] || 'default'}>{v}</Tag> },
    { title: t('act.msg'), dataIndex: 'msg',
      render: (v) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> },
  ]
  const gateCols = [
    { title: t('gate.col.symbol'), dataIndex: 'symbol', width: 72,
      render: (v, r) => (
        <span>
          {v.replace('USDT', '')}
          {!r.in_monitor && <Tag color="default" style={{ marginLeft: 2, fontSize: 9, padding: '0 3px' }}>{t('gate.outPool')}</Tag>}
        </span>
      ) },
    { title: t('gate.col.dir'), dataIndex: 'dir', width: 40,
      render: (v) => <Tag color={v === '多' ? 'red' : 'green'} style={{ margin: 0, padding: '0 4px' }}>{v}</Tag> },
    { title: t('gate.col.ce'), dataIndex: 'ce', width: 52, align: 'center',
      render: (v) => v ? <span style={{ color: 'var(--green, #52c41a)' }}>✓</span> : <span style={{ color: 'var(--mute, #8c8c8c)' }}>✗</span> },
    { title: t('gate.col.ha'), dataIndex: 'ha', width: 52, align: 'center',
      render: (v) => v ? <span style={{ color: 'var(--green, #52c41a)' }}>✓</span> : <span style={{ color: 'var(--mute, #8c8c8c)' }}>✗</span> },
    { title: t('gate.col.bbw'), dataIndex: 'bbw', width: 88, align: 'right',
      render: (v, r) => {
        if (v == null) return <span style={{ color: 'var(--mute, #8c8c8c)' }}>-</span>
        const pass = v >= (r.bbw_thr || 70)
        return <span style={{ color: pass ? 'var(--green, #52c41a)' : 'var(--mute, #8c8c8c)', fontSize: 12 }}>
          {v.toFixed(0)}%<span style={{ fontSize: 9 }}>/{r.bbw_thr || 70}</span>
        </span>
      } },
    { title: t('gate.col.dev'), dataIndex: 'dev', width: 82, align: 'right',
      render: (v, r) => {
        const thr = r.dev_thr || 0
        if (thr <= 0) return <span style={{ color: 'var(--mute, #8c8c8c)' }}>关</span>
        if (v == null) return <span style={{ color: 'var(--mute, #8c8c8c)' }}>-</span>
        const pass = v >= thr
        return <span style={{ color: pass ? 'var(--green, #52c41a)' : 'var(--mute, #8c8c8c)', fontSize: 12 }}>
          {v.toFixed(2)}%<span style={{ fontSize: 9 }}>/{thr}</span>
        </span>
      } },
    { title: t('gate.col.why'), dataIndex: 'why', width: 115,
      render: (v, r) => r.ok
        ? <Tag color="green" style={{ margin: 0 }}>{t('gate.opened')}</Tag>
        : <span style={{ fontSize: 12, color: 'var(--mute, #8c8c8c)', whiteSpace: 'normal' }}>{v}</span> },
  ]
  const evCols = [
    { title: t('ev.col.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('ev.col.cd'), dataIndex: 'minutes', width: 70, align: 'right',
      render: (v) => {
        if (v == null) return '-'
        if (v < 0) return <span style={{ color: 'var(--mute, #8c8c8c)', fontSize: 11 }}>已开始</span>
        const hot = v <= 30
        return <span style={{ color: hot ? 'var(--red, #d4595e)' : undefined, fontWeight: hot ? 600 : 400 }}>
          {fmtCd(v)}
        </span>
      } },
    { title: t('ev.col.imp'), dataIndex: 'importance', width: 54, align: 'center',
      render: (v) => {
        const n = Math.max(1, Math.min(3, Number(v) || 1))
        return <span style={{
          color: n >= 3 ? 'var(--red, #d4595e)' : n === 2 ? 'var(--amber, #d4a95e)' : 'var(--mute, #8c8c8c)',
          fontSize: 11, letterSpacing: '-1px',
        }}>{'★'.repeat(n)}</span>
      } },
    { title: t('ev.col.title'), dataIndex: 'title', ellipsis: true,
      render: (v, r) => (
        <span>
          {lang === 'en' ? (v?.length > 40 ? v.slice(0, 40) + '…' : v) : evName(v)}
          {r.count > 1 && (
            <span style={{ color: 'var(--mute, #8c8c8c)', fontSize: 10 }}> +{r.count - 1}</span>
          )}
        </span>
      ) },
    { title: t('ev.col.gate'), dataIndex: 'in_window', width: 84, align: 'center',
      render: (v) => v
        ? <Tag color="orange" style={{ margin: 0, fontSize: 11 }}>⏸ {t('ev.inWindow')}</Tag>
        : <Tag color="green" style={{ margin: 0, fontSize: 11 }}>{t('ev.open')}</Tag> },
  ]

  const g = gates || {}
  const gateRows = g.rows || []
  const gateBar = g.ts ? new Date(g.ts * 1000).toLocaleTimeString(lang === 'en' ? 'en-US' : 'zh-CN',
    { hour: '2-digit', minute: '2-digit' }) : '-'
  const ev = events || {}
  const evRows = ev.rows || []
  const quietNow = ev.quiet || ''
  // 标签直接带状态：静默时变红字，平时显示事件条数 —— 不用点进去也知道
  const evLabel = quietNow
    ? <span style={{ color: 'var(--red, #d4595e)', fontWeight: 600 }}>⏸ {t('ev.quietNow')}</span>
    : `${t('act.events')}${evRows.length ? ` (${evRows.length})` : ''}`

  // 「经济事件」放到最后一个标签（2026-11-06 浩哥要求）。
  // 它偏参考信息，日常盯的是四道光闸 / 信号流 / 成交记录 / 运行日志。
  const eventsItem = { key: 'events', label: evLabel,
      children: (
        <div>
          <div data-act-status style={{ fontSize: 12, marginBottom: 6 }}>
            {quietNow
              ? <span style={{ color: 'var(--red, #d4595e)', fontWeight: 600 }}>
                  ⏸ {t('ev.quietTitle')}：{evName(quietNow)} —— 暂停开新仓，已有持仓照常管理（止损/平半/时间止损不受影响）
                </span>
              : ev.next_ts
                ? <span style={{ color: 'var(--mute, #8c8c8c)' }}>
                    {t('ev.next')}：<b style={{ color: '#151515' }}>{evName(ev.next_title)}</b>
                    {' '}还有 {fmtCd(ev.next_minutes)} · {t('ev.window')} {ev.window_min} 分钟
                  </span>
                : <span style={{ color: 'var(--mute, #8c8c8c)' }}>{t('ev.none')}</span>}
          </div>
          {evRows.length
            ? <Table rowKey="ts" dataSource={evRows} columns={evCols} size="small"
                     pagination={false} scroll={{ y: scrollY }} />
            : <div style={{ color: 'var(--mute, #8c8c8c)', fontSize: 12, padding: '20px 0', textAlign: 'center' }}>
                {t('ev.none')}
              </div>}
        </div>
      ) }

  const items = [
    { key: 'gates',
      label: `${t('act.gates')} ${gateRows.length ? `(${g.passed || 0}/${gateRows.length})` : ''}`,
      children: (
        <div>
          <div data-act-status style={{ fontSize: 12, color: 'var(--mute, #8c8c8c)', marginBottom: 6 }}>
            {t('gate.bar')} {gateBar} · {t('gate.summary')({
              ce: g.ce_n || 0, ha: g.ha_n || 0, bbw: g.bbw_n || 0,
              dev: g.dev_n || 0, n: gateRows.length,
            })}
          </div>
          <Table rowKey="symbol" dataSource={gateRows} columns={gateCols} size="small"
                 pagination={false} scroll={{ y: scrollY }} />
        </div>
      ) },
    { key: 'signals', label: `${t('act.signals')} (${signals.length})`,
      children: <Table rowKey="id" dataSource={signals} columns={signalCols} size="small"
                       pagination={false} scroll={{ y: scrollY }} /> },
    { key: 'trades', label: `${t('act.trades')} (${trades.length})`,
      children: <Table rowKey="id" dataSource={trades} columns={tradeCols} size="small"
                       pagination={false} scroll={{ y: scrollY }} /> },
    { key: 'logs', label: t('act.logs'),
      children: <Table rowKey="id" dataSource={logs} columns={logCols} size="small"
                       pagination={false} scroll={{ y: scrollY }} /> },
    eventsItem,
  ]
  return (
    <Card size="small" ref={wrapRef}>
      <Tabs items={items} size="small" onChange={onTabChange} />
    </Card>
  )
}
