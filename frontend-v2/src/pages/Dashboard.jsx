import React from 'react'
import { message, Popconfirm } from 'antd'
import { api } from '../api'
import { usePoll } from '../hooks'
import EquityChart from '../panels/EquityChart'
import ServerRack from '../panels/ServerRack'
import ChiefDesk from '../panels/ChiefDesk'
import IntakeRadar from '../panels/IntakeRadar'
import PriceChart from '../panels/PriceChart'
import PoolTable from '../panels/PoolTable'
import ShadowPanel from '../panels/ShadowPanel'
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
  color: '#45e6f5', borderBottom: '1px solid #262a39',
  whiteSpace: 'nowrap', fontSize: 10.5, letterSpacing: '0.04em',
}

const PANEL_TITLE = {
  fontSize: 11.5, fontWeight: 700, color: '#45e6f5',
  marginBottom: 6, letterSpacing: '0.06em',
}

const HEAD_BAR = {
  padding: '7px 12px', borderBottom: '1px solid var(--line)',
  fontSize: 11.5, fontWeight: 700, color: '#45e6f5', letterSpacing: '0.06em',
}

export default function Dashboard({ status }) {
  // 扫描节拍从后端读（配置改了页面自动跟着变），读不到才回落 900s。
  // 2026-11-06 之前这里写死 900，而配置实际是 2m，页面一直在说谎。
  const SCAN_SEC = status?.scan_sec || 900
  const [positions, refresh] = usePoll(api.positions, 5000)
  const [signals] = usePoll(api.signals, 15000)
  const [trades] = usePoll(api.trades, 15000)
  const [logs] = usePoll(api.logs, 15000)
  const [gates] = usePoll(api.gates, 30000)   // 三道光闸：数据 15m 才变一次，30s 够
  const [pool] = usePoll(api.pool, 30000)
  const [shadow] = usePoll(api.shadow, 30000)

  const pos = positions?.positions || []
  const [marginInput, setMarginInput] = React.useState({})
  /* 原位小浮窗状态：{ pid, kind:'lev'|'close'|'margin', lev, frac } */
  const [pop, setPop] = React.useState(null)
  const isPop = (p, kind) => !!(pop && pop.pid === p.id && pop.kind === kind)
  const closePop = () => setPop(null)

  const closeAll = async () => {
    try {
      const r = await api.post('/api/positions/close-all', {})
      message.success('已全平 ' + (r.closed || 0))
      refresh()
    } catch (e) { message.error(e.message) }
  }

  /* ── 杠杆：点按钮直接用原位浮窗确认 ── */
  const askLev = (p, lev) => {
    if (lev === (p.leverage || 3)) { message.info('当前已经是 ' + lev + ' 倍'); return }
    setPop({ pid: p.id, kind: 'lev', lev })
  }
  const doLev = async (p) => {
    const lev = pop.lev
    const sym = (p.symbol || '').replace('USDT', '')
    try {
      await api.post('/api/positions/' + p.id + '/leverage', { leverage: lev })
      message.success(sym + ' 杠杆已调至 ' + lev + 'x')
      closePop(); refresh()
    } catch (e) { message.error(e.message) }
  }

  /* ── 平仓：三个比例按钮各自带原位浮窗 ── */
  const askClose = (p, frac) => setPop({ pid: p.id, kind: 'close', frac })
  const doClose = async (p) => {
    const frac = pop.frac
    const sym = (p.symbol || '').replace('USDT', '')
    try {
      await api.post('/api/positions/' + p.id + '/close', { fraction: frac })
      message.success(sym + ' 已平 ' + (frac * 100).toFixed(0) + '%')
      closePop(); refresh()
    } catch (e) { message.error(e.message) }
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
        <main className="stage" style={{ display: 'flex', flexDirection: 'column', padding: 0 }}>
          {/* 滚动小绿字状态带（参考网站风格） */}
          <div className="ticker">
            <span>
              {(pos.length ? pos.map(p => `${(p.symbol||'').replace('USDT','')} ${p.side === 'long' ? '多' : '空'} ${(p.unrealized_pnl||0) >= 0 ? '+' : ''}${(p.unrealized_pnl||0).toFixed(2)}`).join('  ·  ') : '等待开仓')}
              &nbsp;&nbsp;·&nbsp;&nbsp; S15_CE_ZLSMA 运行中 &nbsp;·&nbsp; 扫描间隔 {SCAN_SEC / 60}m &nbsp;·&nbsp; 15m K线
              &nbsp;&nbsp;·&nbsp;&nbsp; 引擎心跳正常 &nbsp;·&nbsp; 下一轮扫描 {(() => {
                const base = status?.last_scan_ts || status?.last_tick_ts || 0
                const elapsed = Math.floor(Date.now() / 1000) - base
                const left = Math.max(0, SCAN_SEC - (elapsed % SCAN_SEC))
                return left
              })()}s &nbsp;·&nbsp; 请勿关机
            </span>
          </div>


          <div style={{
            display: 'flex', alignItems: 'center', gap: 12, padding: '8px 12px',
            borderBottom: '1px solid var(--line)', flex: '0 0 auto', height: 34,
          }}>
            <b style={{ color: '#45e6f5', fontSize: 12, letterSpacing: '0.08em' }}>持仓操作区</b>
            <span style={{ fontSize: 11, color: '#73788e' }}>{pos.length} 单</span>
            <span style={{ marginLeft: 'auto' }}>
              {pos.length > 0 && (
                <Popconfirm title={'确定全平 ' + pos.length + ' 单？'} onConfirm={closeAll}>
                  <button className="btn" style={{ borderColor: '#ff4d5e', color: '#ff4d5e' }}>
                    一键全平
                  </button>
                </Popconfirm>
              )}
            </span>
          </div>

          <div style={{ flex: '0 0 auto', height: 420, overflow: 'auto' }}>
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
                <tr style={{ position: 'sticky', top: 0, background: '#0b0c12', zIndex: 2 }}>
                  {[
                    ['币种', 'left'], ['方向', 'left'], ['杠杆', 'left'], ['爆仓价', 'right'],
                    ['策略', 'left'], ['开仓价', 'right'], ['现价', 'right'], ['数量', 'right'],
                    ['持仓金额', 'right'], ['保证金', 'right'], ['补保证金', 'center'],
                    ['浮盈', 'right'], ['操作', 'center'],
                  ].map(([h, al]) => (
                    <th key={h} style={{ ...TH, textAlign: al }}>{h}</th>
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
                      </td>
                      <td style={{ padding: '7px 8px', color: long ? '#ff4d5e' : '#35e08a' }}>
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
                                    <b className={(p.liq_dist_pct || 99) < 20 ? 'up' : ''}>
                                      {(p.liq_dist_pct || 0).toFixed(0)}%
                                    </b>
                                  </div>
                                </div>
                              }>
                              <button onClick={() => askLev(p, lv)}
                                style={{
                                  padding: '1px 4px', fontSize: 9.5, cursor: 'pointer',
                                  background: (p.leverage || 3) === lv ? 'rgba(236,79,174,.35)' : 'transparent',
                                  color: (p.leverage || 3) === lv ? '#ff7fcb' : '#73788e',
                                  border: '1px solid ' + ((p.leverage || 3) === lv ? '#ec4fae' : '#262a39'),
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
                          color: (p.liq_dist_pct || 99) < 20 ? '#ff4d5e' : '#6b7183',
                        }}>距 {(p.liq_dist_pct || 0).toFixed(0)}%</div>
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
                      <td style={{ padding: '7px 8px', textAlign: 'right' }}>{fmtPx(p.mark_price)}</td>
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
                        color: win ? '#ff4d5e' : '#35e08a',
                      }}>
                        {win ? '+' : ''}{(p.unrealized_pnl || 0).toFixed(2)}
                        <span style={{ fontSize: 9, opacity: .55, marginLeft: 1 }}>U</span>
                      </td>
                      <td style={{ padding: '6px 8px' }}>
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
                                    <b className={(p.unrealized_pnl || 0) >= 0 ? 'up' : 'dn'}>
                                      {(p.unrealized_pnl || 0) >= 0 ? '+' : ''}
                                      {(p.unrealized_pnl || 0).toFixed(2)}U
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
          <div className="stats">
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
                  {/* 三大项：净值占左大块，右侧上下两项 */}
                  <div className="stats-big3">
                    <div className="sbig equity">
                      <div className="chief-wrap"><ChiefDesk width={299} /></div>
                      <div className="radar-wrap"><IntakeRadar width={124} /></div>
                      <div className="lb">账 户 净 值</div>
                      <div className="val"><PopNum text={equity.toFixed(2)} /><small>U</small></div>
                      <div className="sub">EQUITY · {status?.mode === 'live' ? 'LIVE 实盘' : 'PAPER 模拟盘'}</div>
                    </div>
                    <div className={'sbig pnl ' + (totalUpnl >= 0 ? 'c-red' : 'c-green')}>
                      <div className="lb">浮 动 盈 亏</div>
                      <div className="val"><PopNum text={(totalUpnl >= 0 ? '+' : '') + totalUpnl.toFixed(2)} /><small>U</small></div>
                    </div>
                    <div className={'sbig real ' + (realized >= 0 ? 'c-red' : 'c-green')}>
                      <div className="lb">今 日 已 实 现</div>
                      <div className="val"><PopNum text={(realized >= 0 ? '+' : '') + realized.toFixed(2)} /><small>U</small></div>
                    </div>
                  </div>

                  {/* 五项小数据 */}
                  <div className="stats-small5">
                    <div className="ssm">
                      <div className="lb">资金使用率</div>
                      <div className="val c-cyan">{marginUse.toFixed(1)}<small>%</small></div>
                      <div className="bar-line">
                        <i style={{
                          width: `${Math.min(100, marginUse)}%`,
                          background: marginUse < 40 ? '#45e6f5' : marginUse < 60 ? '#ffb454' : '#ff4d5e',
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
            <ServerRack width={52} />
          </div>

        </main>

        <aside className="side">
          <div style={{
            position: 'relative', background: 'var(--panel)',
            border: '1px solid var(--line)', borderRadius: 5, overflow: 'hidden',
          }}>
            <div style={HEAD_BAR}>账户权益曲线</div>
            <EquityChart activeMode={status?.mode} />
          </div>
          <div style={{
            background: 'var(--panel)', border: '1px solid var(--line)',
            borderRadius: 5, overflow: 'hidden', display: 'flex', flexDirection: 'column',
          }}>
            <div style={HEAD_BAR}>行情与开平仓标记</div>
            <div style={{ flex: 1, minHeight: 0 }}>
              <PriceChart universe={status?.universe || []} />
            </div>
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
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
            <ActivityTabs
              signals={signals?.signals || []}
              trades={trades?.trades || []}
              logs={logs?.logs || []}
              gates={gates}
            />
          </div>
        </article>
      </section>
    </>
  )
}
