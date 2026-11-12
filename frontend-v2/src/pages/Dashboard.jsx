import React from 'react'
import { message, Popconfirm } from 'antd'
import { api, READ_ONLY } from '../api'
import { usePoll } from '../hooks'
import EquityChart from '../panels/EquityChart'
import ServerRack from '../panels/ServerRack'
import PriceChart from '../panels/PriceChart'
import PoolTable from '../panels/PoolTable'
import ShadowPanel from '../panels/ShadowPanel'
import RadarPanel from '../panels/RadarPanel'
import ActivityTabs from '../panels/ActivityTabs'

function fmtPx(v) {
  if (v == null) return '-'
  const a = Math.abs(v)
  if (a >= 1000) return v.toFixed(1)
  if (a >= 1) return v.toFixed(4)
  return v.toPrecision(4)
}


/* 数字动效：值变化时上滑 + 模糊入场 + 回弹 */
function PopNum({ text, className }) {
  const [k, setK] = React.useState(0)
  const prev = React.useRef(text)
  React.useEffect(() => {
    if (prev.current !== text) { prev.current = text; setK(x => x + 1) }
  }, [text])
  return <span key={k} className={'popnum ' + (className || '')}>{text}</span>
}

function fmtQty(v) {
  if (v == null) return '-'
  const a = Math.abs(v)
  if (a >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 0 })
  if (a >= 100) return v.toFixed(2)
  if (a >= 1) return v.toFixed(3)
  return v.toPrecision(4)
}

const TH = {
  padding: '7px 8px', textAlign: 'left', fontWeight: 700,
  color: 'var(--cyan, #45e6f5)', borderBottom: '1px solid #262a39',
  whiteSpace: 'nowrap', fontSize: 10.5, letterSpacing: '0.04em',
}

// 列头芯片糖果色 —— 13 列各一色，取自 agent-display 的 agent 调色板
const TH_CHIP = [
  '#a774ff', '#2fd27a', '#ff9a1f', '#ff4458', '#3b7cff',
  '#25d6f0', '#ffd23f', '#ff5cae', '#b5ef3a', '#62e6c7',
  '#ff7b54', '#ff3f8e', '#f3f4f8',
]

const PANEL_TITLE = {
  fontSize: 11.5, fontWeight: 700, color: 'var(--cyan, #45e6f5)',
  marginBottom: 6, letterSpacing: '0.06em',
}

// 2026-11-06 删除了 HEAD_BAR 常量：右侧两张卡的标题已搬进各自的 Card head
// （见 panels/EquityChart.jsx、panels/PriceChart.jsx 的 CARD_TITLE），
// 外层不再有独立标题条，这个常量没有使用者了。

export default function Dashboard({ status }) {
  // 扫描节拍从后端读（配置改了页面自动跟着变），读不到才回落 900s。
  // 2026-11-06 之前这里写死 900，而配置实际是 2m，页面一直在说谎。
  const SCAN_SEC = status?.scan_sec || 900
  const [positions, refresh] = usePoll(api.positions, 5000)
  const [signals] = usePoll(api.signals, 15000)
  const [trades] = usePoll(api.trades, 15000)
  const [logs] = usePoll(api.logs, 15000)
  const [gates] = usePoll(api.gates, 30000)   // 七道进场闸：K 线 15m 才变一次，30s 够
  const [events] = usePoll(api.events, 60000) // 经济事件：静默窗口要准，1min 刷一次
  const [pool] = usePoll(api.pool, 30000)
  const [shadow] = usePoll(api.shadow, 30000)
  // 持仓强弱雷达：独立侧车，15 秒刷一次（后端每分钟算一次，够用）
  const [radar] = usePoll(api.radar, 15000)
  const radarMap = React.useMemo(() => {
    const m = {}
    ;(radar?.rows || []).forEach((r) => { m[r.symbol] = r })
    return m
  }, [radar])

  const pos = positions?.positions || []
  const [marginInput, setMarginInput] = React.useState({})
  /* 原位小浮窗状态：{ pid, kind:'lev'|'close'|'margin', lev, frac } */
  const [pop, setPop] = React.useState(null)
  const isPop = (p, kind) => !!(pop && pop.pid === p.id && pop.kind === kind)
  const closePop = () => setPop(null)

  /* 请求进行中标记：防止双击把同一个平仓发两次。
     后端有 per-pid 锁 + status='open' 条件兜底，但「两个 50% 请求串行」
     本身就会卖到 75% —— 用户只点了一次，不该承担这个后果。 */
  const [busy, setBusy] = React.useState({})
  const keyOf = (p, kind) => p.id + '-' + kind
  const isBusy = (p, kind) => !!busy[keyOf(p, kind)]
  const markBusy = (p, kind, v) => setBusy((s) => {
    const n = { ...s }
    if (v) n[keyOf(p, kind)] = true
    else delete n[keyOf(p, kind)]
    return n
  })
  const [closingAll, setClosingAll] = React.useState(false)

  const closeAll = async () => {
    if (closingAll) return
    setClosingAll(true)
    try {
      const r = await api.post('/api/positions/close-all', {})
      let msg = '已全平 ' + (r.closed || 0) + ' 个'
      if (r.paused_today) msg += '，今日不再自动开新仓'
      message.success(msg)
      refresh()
    } catch (e) {
      // 部分失败时后端回 ok:false + closed/failed，必须如实说清楚
      const pay = e.payload
      if (pay && pay.closed > 0) {
        message.warning(`平掉 ${pay.closed} 个，${pay.failed} 个没成功 — ${e.message}`)
      } else {
        message.error(e.message)
      }
      refresh()          // 无论成败都刷新，让用户看到真实持仓
    } finally { setClosingAll(false) }
  }

  /* ── 杠杆：点按钮直接用原位浮窗确认 ── */
  const askLev = (p, lev) => {
    if (lev === (p.leverage || 3)) { message.info('当前已经是 ' + lev + ' 倍'); return }
    setPop({ pid: p.id, kind: 'lev', lev })
  }
  const doLev = async (p) => {
    if (isBusy(p, 'lev')) return
    const lev = pop.lev
    const sym = (p.symbol || '').replace('USDT', '')
    markBusy(p, 'lev', true)
    try {
      await api.post('/api/positions/' + p.id + '/leverage', { leverage: lev })
      message.success(sym + ' 杠杆已调至 ' + lev + 'x')
      closePop(); refresh()
    } catch (e) { message.error(e.message) }
    finally { markBusy(p, 'lev', false) }
  }

  /* ── 平仓：三个比例按钮各自带原位浮窗 ── */
  const askClose = (p, frac) => setPop({ pid: p.id, kind: 'close', frac })
  const doClose = async (p) => {
    if (isBusy(p, 'close')) return      // 双击直接吞掉
    const frac = pop.frac
    const sym = (p.symbol || '').replace('USDT', '')
    markBusy(p, 'close', true)
    try {
      await api.post('/api/positions/' + p.id + '/close', { fraction: frac })
      message.success(sym + ' 已平 ' + (frac * 100).toFixed(0) + '%')
      closePop(); refresh()
    } catch (e) {
      // ok:false 也会走到这里（平仓失败 / 已被别的路径平掉），不能弹成功
      message.error(e.message)
      closePop(); refresh()
    } finally { markBusy(p, 'close', false) }
  }

  /* ── 补保证金：取消时清空输入框 ── */
  const askMargin = (p) => {
    const amt = parseFloat(marginInput[p.id])
    if (!amt || amt <= 0) return
    setPop({ pid: p.id, kind: 'margin' })
  }
  const doMargin = async (p) => {
    const amt = parseFloat(marginInput[p.id])
    const sym = (p.symbol || '').replace('USDT', '')
    try {
      await api.post('/api/positions/' + p.id + '/margin', { amount: amt })
      message.success('已追加 ' + amt + 'U')
      setMarginInput(s => ({ ...s, [p.id]: '' }))
      closePop(); refresh()
    } catch (e) { message.error(e.message) }
  }
  /* 取消 / 点浮窗外部：清空该行输入 */
  const cancelMargin = (p) => {
    setMarginInput(s => ({ ...s, [p.id]: '' }))
    closePop()
  }


  return (
    <>
      <section className="mid">
        <main className="stage" style={{ display: 'flex', flexDirection: 'column', padding: 0, position: 'relative' }}>
          {/* 滚动小绿字状态带（参考网站风格） */}
          <div className="ticker">
            <span>
              {(pos.length ? pos.map(p => `${(p.symbol||'').replace('USDT','')} ${p.side === 'long' ? '多' : '空'} ${p.stale ? '缺价' : ((p.unrealized_pnl||0) >= 0 ? '+' : '') + (p.unrealized_pnl||0).toFixed(2)}`).join('  ·  ') : '等待开仓')}
              &nbsp;&nbsp;·&nbsp;&nbsp; S15_CE_ZLSMA 运行中 &nbsp;·&nbsp; 扫描间隔 {SCAN_SEC / 60}m &nbsp;·&nbsp; 15m K线
              &nbsp;&nbsp;·&nbsp;&nbsp; 引擎心跳正常 &nbsp;·&nbsp; 下一轮扫描 {(() => {
                const base = status?.last_scan_ts || status?.last_tick_ts || 0
                const elapsed = Math.floor(Date.now() / 1000) - base
                const left = Math.max(0, SCAN_SEC - (elapsed % SCAN_SEC))
                return left
              })()}s &nbsp;·&nbsp; 请勿关机
            </span>
          </div>


          {/* 「持仓操作区 0 单」整行已取消；一键全平收成小胶囊，浮在状态带右侧，仅持仓时出现 */}
          {/* 只读观战模式（?ro=1）下不渲染任何平仓入口 —— 后端也会 401，双保险 */}
          {/* ★ class="close-all-wrap" 不能省：v2.css 靠 class 定位，用 nth-child 会因条件渲染错位 */}
          {pos.length > 0 && !READ_ONLY && (
            <div className="close-all-wrap" style={{ position: 'absolute', top: 3, right: 8, zIndex: 6 }}>
              <Popconfirm title={'确定全平 ' + pos.length + ' 单？'} onConfirm={closeAll}
                okText={closingAll ? '正在平…' : '确定'}
                okButtonProps={{ loading: closingAll, disabled: closingAll }}>
                <button className="btn close-all-fab"
                  disabled={closingAll}
                  style={{
                    borderColor: 'var(--red, #ff4d5e)', color: 'var(--red, #ff4d5e)',
                    opacity: closingAll ? 0.6 : 1,
                    cursor: closingAll ? 'not-allowed' : 'pointer',
                  }}>
                  {closingAll ? '平仓中…' : '一键全平'}
                </button>
              </Popconfirm>
            </div>
          )}

          {/* ★ 2026-11-09 浩哥定：去掉滑块。原先是写死 height:420 + overflow:auto，
              持仓行高 49px，满 6 个时 6×49+4×36+34=472 > 420 → 冒出一条滚动条。
              改成按内容自适应：表格有多少行就多高，父级 .stage 是 flex column，
              下面还有空间（stage 718 = 表 420 + 雷达 176），撑开也挤不坏。 */}
          <div className="pos-table" style={{ flex: '0 0 auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5, tableLayout: 'fixed' }}>
              <colgroup>
                <col style={{ width: '5.5%' }} />
                <col style={{ width: '4.0%' }} />
                <col style={{ width: '13.4%' }} />
                <col style={{ width: '6.4%' }} />
                <col style={{ width: '4.8%' }} />
                <col style={{ width: '7.6%' }} />
                <col style={{ width: '7.6%' }} />
                <col style={{ width: '7.0%' }} />
                <col style={{ width: '7.2%' }} />
                <col style={{ width: '6.2%' }} />
                <col style={{ width: '7.8%' }} />
                <col style={{ width: '7.6%' }} />
                <col style={{ width: '14.9%' }} />
              </colgroup>
              <thead>
                <tr style={{ position: 'sticky', top: 0, background: 'var(--panel)', zIndex: 2 }}>
                  {[
                    ['币种', 'left'], ['方向', 'left'], ['杠杆', 'left'], ['爆仓价', 'right'],
                    ['策略', 'left'], ['开仓价', 'right'], ['现价', 'right'], ['数量', 'right'],
                    ['持仓金额', 'right'], ['保证金', 'right'], ['补保证金', 'center'],
                    ['浮盈', 'right'], ['操作', 'center'],
                  ].map(([h, al], i) => (
                    <th key={h} style={{ ...TH, textAlign: al }}>
                      <span
                          className={'th-chip' + (h === '操作' ? ' th-chip-op' : '')}
                          style={{ '--c': TH_CHIP[i % TH_CHIP.length] }}
                        >{h}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pos.map((p) => {
                  const long = p.side === 'long'
                  const win = (p.unrealized_pnl || 0) >= 0
                  return (
                    <tr key={p.id} style={{ borderBottom: '1px solid #151824' }}>
                      <td style={{ padding: '7px 8px', fontWeight: 700 }}>
                        {(p.symbol || '').replace('USDT', '')}
                        {(() => {
                          const r = radarMap[p.symbol]
                          if (!r) return null
                          const sc = Number(r.score) || 0
                          // 档位用 class 控制配色（纸面主题要压深，不能靠内联色）
                          const lv = sc >= 80 ? 'lv4' : sc >= 60 ? 'lv3' : sc >= 40 ? 'lv2' : 'lv1'
                          // 趋势箭头：跟自己上一条比，变化 <2 分算走平
                          const prev = r.history && r.history[1] ? Number(r.history[1].score) : null
                          const arrow = prev == null ? '' : sc > prev + 2 ? '↑' : sc < prev - 2 ? '↓' : '→'
                          return (
                            <span
                              className={`rdot ${lv}`}
                              title={`雷达 ${sc.toFixed(1)} 分 · ${r.verdict}
${r.advice}`}
                            >
                              {Math.round(sc)}{arrow}
                            </span>
                          )
                        })()}
                      </td>
                      <td style={{ padding: '7px 8px', color: long ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)' }}>
                        {long ? '多' : '空'}
                        {p.half_closed ? (
                          <span
                            title="已到 2R 平掉一半，剩下一半继续跑（数量列是剩余量）"
                            style={{
                              marginLeft: 5, fontSize: 10, lineHeight: '14px',
                              padding: '1px 4px', borderRadius: 3,
                              background: 'rgba(255,193,7,.16)',
                              color: '#ffc107', fontWeight: 700,
                              border: '1px solid rgba(255,193,7,.35)',
                              verticalAlign: 'middle',
                            }}
                          >½</span>
                        ) : null}
                      </td>
                      <td style={{ padding: '7px 8px' }}>
                        <span style={{ display: 'inline-flex', gap: 2 }}>
                          {[3, 6, 10, 20].map((lv) => (
                            <Popconfirm key={lv}
                              open={isPop(p, 'lev') && pop.lev === lv}
                              onOpenChange={(o) => { if (!o) closePop() }}
                              onConfirm={() => doLev(p)}
                              onCancel={closePop}
                              okText="确认调整" cancelText="取消"
                              placement="top" overlayClassName="pop-dark"
                              description={
                                <div className="pop-body">
                                  <div className="pop-h">
                                    {(p.symbol || '').replace('USDT', '')} · 调整杠杆
                                  </div>
                                  <div className="pop-row">
                                    <span>杠杆</span>
                                    <b>{p.leverage || 3}倍 → {lv}倍</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>保证金</span>
                                    <b>{(p.margin || 0).toFixed(0)}U 不变</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>名义价值</span>
                                    <b>{(p.notional || 0).toFixed(0)}U</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>距爆仓</span>
                                    <b className={(!p.stale && (p.liq_dist_pct || 99) < 20) ? 'up' : ''}>
                                      {p.stale ? '--' : (p.liq_dist_pct || 0).toFixed(0) + '%'}
                                    </b>
                                  </div>
                                </div>
                              }>
                              <button onClick={() => askLev(p, lv)}
                                style={{
                                  padding: '1px 4px', fontSize: 9.5, cursor: 'pointer',
                                  background: (p.leverage || 3) === lv ? 'rgba(236,79,174,.35)' : 'transparent',
                                  color: (p.leverage || 3) === lv ? 'var(--joint, #ff7fcb)' : 'var(--mute, #73788e)',
                                  border: '1px solid ' + ((p.leverage || 3) === lv ? 'var(--joint, #ec4fae)' : 'var(--line, #262a39)'),
                                  borderRadius: 2,
                                }}>{lv}x</button>
                            </Popconfirm>
                          ))}
                        </span>
                      </td>
                      <td style={{ padding: '6px 8px', textAlign: 'right', lineHeight: 1.25 }}>
                        <div style={{ fontVariantNumeric: 'tabular-nums' }}>
                          {p.liq_price ? fmtPx(p.liq_price) : '-'}
                        </div>
                        <div style={{
                          fontSize: 9,
                          color: (!p.stale && (p.liq_dist_pct || 99) < 20) ? 'var(--red, #ff4d5e)' : 'var(--mute, #6b7183)',
                        }}>距 {p.stale ? '--' : (p.liq_dist_pct || 0).toFixed(0) + '%'}</div>
                      </td>
                      <td style={{ padding: '7px 8px' }}>
                        <span style={{
                          fontSize: 10, padding: '1px 5px', borderRadius: 2,
                          color: '#9aa0b5', border: '1px solid #262a39',
                          fontFamily: 'Cascadia Mono, Consolas, monospace',
                          letterSpacing: '.04em',
                        }}>S15</span>
                      </td>
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{fmtPx(p.entry_price)}</td>
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{p.stale ? '--' : fmtPx(p.mark_price)}</td>
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{fmtQty(p.qty)}</td>
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{(p.notional || 0).toFixed(0)}U</td>
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{(p.margin || 0).toFixed(0)}U</td>
                      <td style={{ padding: '7px 8px' }}>
                        <Popconfirm
                          open={isPop(p, 'margin')}
                          onOpenChange={(o) => { if (!o) cancelMargin(p) }}
                          onConfirm={() => doMargin(p)}
                          onCancel={() => cancelMargin(p)}
                          okText="确认追加" cancelText="取消"
                          placement="top" overlayClassName="pop-dark"
                          description={
                            <div className="pop-body">
                              <div className="pop-h">
                                {(p.symbol || '').replace('USDT', '')} · 补保证金
                              </div>
                              <div className="pop-row">
                                <span>追加</span>
                                <b className="cy">{parseFloat(marginInput[p.id]) || 0}U</b>
                              </div>
                              <div className="pop-row">
                                <span>保证金</span>
                                <b>{(p.margin || 0).toFixed(0)}U → {((p.margin || 0) + (parseFloat(marginInput[p.id]) || 0)).toFixed(0)}U</b>
                              </div>
                              <div className="pop-warn">爆仓价会拉远，可用保证金相应减少</div>
                            </div>
                          }>
                          <input
                            placeholder="+ 补保" title="补保证金降杠杆（不会增加持仓数量，回车确认）"
                            value={marginInput[p.id] || ''}
                            onChange={e => setMarginInput(s => ({ ...s, [p.id]: e.target.value }))}
                            onKeyDown={e => {
                              if (e.key === 'Enter') { e.preventDefault(); askMargin(p) }
                              if (e.key === 'Escape') { cancelMargin(p) }
                            }}
                            style={{
                              width: 62, padding: '3px 5px', fontSize: 10,
                              background: 'rgba(69,230,245,.06)',
                              border: '1px solid rgba(69,230,245,.32)',
                              borderRadius: 2, color: '#eef0f7', outline: 'none',
                              textAlign: 'center',
                            }} />
                        </Popconfirm>
                      </td>
                      <td style={{
                        padding: '7px 8px', textAlign: 'right', fontWeight: 700,
                        color: p.stale ? 'var(--mute, #6b7183)'
                          : (win ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)'),
                      }}>
                        {p.stale ? '--' : (win ? '+' : '') + (p.unrealized_pnl || 0).toFixed(2)}
                        {!p.stale && (
                          <span style={{ fontSize: 9, opacity: .55, marginLeft: 1 }}>U</span>
                        )}
                      </td>
                      <td style={{ padding: '6px 8px' }}>
                        {READ_ONLY ? (
                          <span style={{ fontSize: 11, opacity: .45 }}>观战</span>
                        ) : (
                        <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                          {[
                            { f: 1, label: '全平', main: true },
                            { f: 0.5, label: '½', main: false },
                            { f: 1 / 3, label: '⅓', main: false },
                          ].map((o) => (
                            <Popconfirm key={o.label}
                              open={isPop(p, 'close') && Math.abs((pop.frac || 0) - o.f) < 1e-9}
                              onOpenChange={(x) => { if (!x) closePop() }}
                              onConfirm={() => doClose(p)}
                              onCancel={closePop}
                              okText="确认平仓" cancelText="取消"
                              okButtonProps={{
                                loading: isBusy(p, 'close'),
                                disabled: isBusy(p, 'close'),
                              }}
                              cancelButtonProps={{ disabled: isBusy(p, 'close') }}
                              placement="top" overlayClassName="pop-dark"
                              description={
                                <div className="pop-body">
                                  <div className="pop-h">
                                    {(p.symbol || '').replace('USDT', '')} ·{' '}
                                    {p.side === 'long' ? '多' : '空'} 平仓
                                  </div>
                                  <div className="pop-row">
                                    <span>平仓比例</span>
                                    <b className={o.main ? 'up' : 'cy'}>{(o.f * 100).toFixed(0)}%</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>数量</span>
                                    <b>{fmtQty(p.qty)} → {fmtQty((p.qty || 0) * (1 - o.f))}</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>保证金</span>
                                    <b>{(p.margin || 0).toFixed(0)}U → {((p.margin || 0) * (1 - o.f)).toFixed(0)}U</b>
                                  </div>
                                  <div className="pop-row">
                                    <span>浮盈</span>
                                    <b className={p.stale ? '' : ((p.unrealized_pnl || 0) >= 0 ? 'up' : 'dn')}>
                                      {p.stale ? '--' : ((p.unrealized_pnl || 0) >= 0 ? '+' : '')
                                        + (p.unrealized_pnl || 0).toFixed(2) + 'U'}
                                    </b>
                                  </div>
                                  <div className="pop-warn">
                                    {o.main ? '整单平仓，执行后不可撤销' : `仅平 ${(o.f * 100).toFixed(0)}%，剩余继续持有`}
                                  </div>
                                </div>
                              }>
                              <button
                                className={'cbtn ' + (o.main ? 'cbtn-full' : 'cbtn-part')}
                                style={{ flex: o.main ? 1.45 : 1 }}
                                onClick={() => askClose(p, o.f)}>{o.label}</button>
                            </Popconfirm>
                          ))}
                        </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
                {/* 补足到 10 行的空位（保持表格高度稳定） */}
                {Array.from({ length: Math.max(0, 10 - pos.length) }).map((_, i) => (
                  <tr key={'empty' + i} style={{ borderBottom: '1px solid #151824', height: 36 }}>
                    <td colSpan={13} style={{ height: 36 }} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* ══════════ 下半：8 项数据统一排版（无框体，直接压空白）══════════ */}
          <div className="stats" data-empty={pos.length ? '0' : '1'}>
            {(() => {
              const totalMargin = pos.reduce((s, p) => s + (p.margin || 0), 0)
              const totalUpnl = pos.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)
              const equity = status?.equity ?? 0
              const realized = status?.daily_realized_pnl ?? 0
              const marginUse = equity > 0 ? (totalMargin / equity) * 100 : 0
              const marginFree = equity - totalMargin
              const nLong = pos.filter((p) => p.side === 'long').length
              const nShort = pos.length - nLong
              const liqs = pos.map((p) => p.liq_dist_pct).filter((v) => v != null)
              const minLiq = liqs.length ? Math.min(...liqs) : null
              const avgAge = pos.length
                ? pos.reduce((s, p) => s + (p.age_sec || 0), 0) / pos.length / 3600
                : 0
              return (
                <>
                  {/* 持仓强弱雷达 —— 取代原来的「净值 / 浮动盈亏 / 今日已实现」三项 */}
                  <RadarPanel rows={radar && radar.rows} />

                  {/* 五项小数据 */}
                  <div className="stats-small5">
                    <div className="ssm">
                      <div className="lb">资金使用率</div>
                      <div className="val c-cyan">{marginUse.toFixed(1)}<small>%</small></div>
                      <div className="bar-line">
                        <i style={{
                          width: `${Math.min(100, marginUse)}%`,
                          background: marginUse < 40 ? 'var(--cyan, #45e6f5)' : marginUse < 60 ? 'var(--amber, #ffb454)' : 'var(--red, #ff4d5e)',
                        }} />
                      </div>
                    </div>
                    <div className="ssm">
                      <div className="lb">可用保证金</div>
                      <div className="val plain">{marginFree.toFixed(0)}<small>U</small></div>
                    </div>
                    <div className="ssm">
                      <div className="lb">持仓数 · 多空比</div>
                      <div className="val plain">
                        {pos.length} <small className="c-red">· 多{nLong}</small> <small className="c-green">空{nShort}</small>
                      </div>
                    </div>
                    <div className="ssm">
                      <div className="lb">最近爆仓距离</div>
                      <div className={'val ' + (minLiq != null && minLiq < 25 ? 'c-red' : 'c-cyan')}>
                        {minLiq != null ? minLiq.toFixed(1) : '-'}<small>%</small>
                      </div>
                    </div>
                    <div className="ssm">
                      <div className="lb">平均持仓</div>
                      <div className="val plain">{avgAge.toFixed(1)}<small>h</small></div>
                    </div>
                  </div>
                </>
              )
            })()}
          </div>

          {/* 四角折角标（照样板 .pnl::before/::after 的 L 型，扩到 4 个角） */}
          <i className="cnr cnr-tl" /><i className="cnr cnr-tr" />
          <i className="cnr cnr-bl" /><i className="cnr cnr-br" />

          {/* 服务器机柜（照原版 canvas 绘制坐标复刻：14 机位 + GPU ×8） */}

          <div className="srv-wrap">
            <ServerRack width={62} />
          </div>

        </main>

        <aside className="side">
          <div style={{
            // 外层只做定位 + 圆角裁切；框和背景由内部 Card 承担（标题已搬进 Card head）
            position: 'relative', overflow: 'hidden', borderRadius: 5,
          }}>
            <EquityChart activeMode={status?.mode}
                          upnl={pos.reduce((a, p) => a + (p.unrealized_pnl || 0), 0)}
                          realized={status?.daily_realized_pnl ?? 0} />
          </div>
          <div style={{
            // 同上：外层只做定位 + 圆角裁切，标题和框都在 Card 里
            position: 'relative', overflow: 'hidden', borderRadius: 5,
          }}>
            <PriceChart universe={status?.universe || []} />
          </div>
        </aside>
      </section>

      <section className="bot">
        <article className="pnl">
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
            <PoolTable pool={pool} />
          </div>
        </article>
        <article className="pnl">
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
            <ShadowPanel shadow={shadow} />
          </div>
        </article>
        <article className="pnl">
          <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <ActivityTabs
              signals={signals?.signals || []}
              trades={trades?.trades || []}
              logs={logs?.logs || []}
              gates={gates}
              events={events}
            />
          </div>
        </article>
      </section>
    </>
  )
}
