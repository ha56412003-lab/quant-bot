import ThemeSwitch from '../ThemeSwitch'
import { useEffect, useRef, useState } from 'react'
import '../spider.css'
import avatars from '../avatars.json'

function useFitScale() {
  const ref = useRef(null)
  const [s, setS] = useState(0.5)
  useEffect(() => {
    const calc = () => {
      const el = ref.current
      if (!el) return
      setS(Math.min(el.clientWidth / 1920, el.clientHeight / 1080))
    }
    calc()
    window.addEventListener('resize', calc)
    const t = setInterval(calc, 800)
    return () => { window.removeEventListener('resize', calc); clearInterval(t) }
  }, [])
  return [ref, s]
}

function fmtPx(v) {
  if (v == null) return '-'
  const a = Math.abs(v)
  if (a >= 1000) return v.toFixed(1)
  if (a >= 1) return v.toFixed(4)
  return v.toPrecision(4)
}

// 每个币种一个固定且互不重复的头像：
// 把当前所有持仓币种按字典序排好，第 k 个币种取 avatars[k]。
// 同币种永远同一个头像；持仓上限 10 < 头像总数 14，所以必定全不重复。
function buildAvatarMap(positions) {
  const syms = [...new Set(positions.map((p) => (p.symbol || '').replace('USDT', '')))].sort()
  const map = {}
  syms.forEach((sym, k) => { map[sym] = avatars[k % avatars.length] })
  return map
}

// 空卡用排最后的那几个头像，避免和实卡撞脸
function emptyAvatar(k, usedCount) {
  return avatars[(usedCount + k) % avatars.length]
}

/* ── 持仓卡：四行结构 ── */
function PosCard({ p, avatar }) {
  const long = p.side === 'long'
  const entry = p.entry_price
  const px = p.mark_price
  const sl = p.stop_price
  const upnl = p.unrealized_pnl || 0
  const win = upnl >= 0
  const sym = (p.symbol || '').replace('USDT', '')

  /* ── 出场进度（2026-11-06 重做）────────────────────────────────
     旧版拿 take_profit 当终点算进度：wMax = tp - entry。
     出场规则改成「2R 平一半、剩一半放开跑」后 take_profit = 0，
     wMax 变成负数，`wMax > 0` 不成立 → prog 恒为 0，条永远是空的。

     现在按新规则分三态，条的「终点」跟着状态走：
       loss  亏损中     → 终点是止损，看「还有多远爆仓」
       run   +0~2R      → 终点是 2R，看「离平半还差多少」
       free  ≥2R/已平半  → 跑道，看「峰顶跑多远 + 回撤离 0.8R 多远」
  */
  const risk = (entry && sl) ? Math.abs(entry - sl) : 0     // 1R 的价格距离
  const gain = (entry && px) ? (long ? px - entry : entry - px) : 0
  const curR = risk > 0 ? gain / risk : 0

  // 峰顶 R：mfe_pct 是最大有利偏移百分比，反推价格距离再除以 1R
  const mfePct = Math.abs(p.mfe_pct || 0)
  const peakR = (risk > 0 && entry) ? (mfePct / 100 * entry) / risk : 0

  const halfDone = !!p.half_closed
  const phase = curR < 0 ? 'loss' : (halfDone || curR >= 2) ? 'free' : 'run'
  const stopPct = (px && sl) ? Math.abs(px - sl) / px * 100 : 0
  const drawdownR = Math.max(0, peakR - curR)               // 距峰顶回撤了多少 R
  const ddNear = phase === 'free' && drawdownR >= 0.6       // 逼近 0.8R 触发线
  const stopNear = phase === 'loss' && Math.abs(curR) >= 0.7 // 逼近止损

  // 亏损：从右往左填，右侧是开仓位
  const lossFill = Math.min(1, Math.abs(curR))
  // 冲刺 2R：从左往右填
  const runFill = Math.min(1, curR / 2)
  // 跑道：条代表「2R → 峰顶」这一段，游标是当前价
  const runSpan = Math.max(0.01, peakR - 2)
  const freeCursor = Math.max(0, Math.min(1, (curR - 2) / runSpan))

  const ageH = (p.age_sec || 0) / 3600
  const maxH = (p.max_hold_sec || 1) / 3600
  const retPct = entry ? ((long ? px - entry : entry - px) / entry) * 100 : 0

  return (
    <article className="card work" style={{ '--c': win ? '#ff4d5e' : '#35e08a' }}>
      {/* 行1：头像 + 币种 + 方向标签 + 浮盈 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <img className="av" src={avatar} alt="" />
        <b style={{ fontSize: 15, letterSpacing: '0.05em', whiteSpace: 'nowrap' }}>{sym}</b>
        <em className="tg" style={{
          color: long ? '#ff4d5e' : '#35e08a',
          borderColor: long ? 'rgba(255,77,94,.55)' : 'rgba(53,224,138,.55)',
        }}>{long ? 'LONG' : 'SHORT'}</em>
        <span key={Math.round(upnl * 100)} className="pnl-num" style={{
          marginLeft: 'auto', fontWeight: 900,
          fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
          color: win ? '#ff4d5e' : '#35e08a',
        }}>
          {win ? '+' : ''}{upnl.toFixed(2)}
        </span>
      </div>

      {/* 行2：开仓价 → 现价 + 收益率 */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        fontSize: 11, color: '#8b90a6', fontVariantNumeric: 'tabular-nums',
      }}>
        <span style={{ whiteSpace: 'nowrap' }}>{fmtPx(entry)} → {fmtPx(px)}</span>
        <span style={{
          fontWeight: 700, whiteSpace: 'nowrap',
          color: win ? '#ff4d5e' : '#35e08a',
        }}>
          {retPct >= 0 ? '+' : ''}{retPct.toFixed(2)}%
        </span>
      </div>

      {/* 行3：出场进度条 —— 终点随状态变（止损 / 2R / 峰顶跑道） */}
      <div className={`bar prog ${phase}${stopNear ? ' near' : ''}${ddNear ? ' near' : ''}`}>
        {/* 亏损：左侧留空，右侧填满到开仓位；左端画止损锚点 */}
        {phase === 'loss' && (
          <>
            <i style={{ left: `${(1 - lossFill) * 100}%`, width: `${lossFill * 100}%` }} />
            <span className="stop-anchor" />
          </>
        )}
        {/* 冲刺 2R：从左往右 */}
        {phase === 'run' && (
          <i style={{ left: 0, width: `${runFill * 100}%` }} />
        )}
        {/* 跑道：整条填满底色，中间一根游标表示当前价在 2R↑峰顶 之间的位置 */}
        {phase === 'free' && (
          <>
            <i className="track-all" />
            <b className="cursor" style={{ left: `${freeCursor * 100}%` }} />
            <span className="peak-mark" />
          </>
        )}
      </div>

      {/* 行4：状态数字（主位）+ 峰谷 + 持仓时间 */}
      <div className="c4">
        <span className={`prog-txt ${phase}${stopNear || ddNear ? ' danger' : ''}`}>
          {phase === 'loss' && <>距止损 <b>{stopPct.toFixed(1)}%</b></>}
          {phase === 'run' && <><b>+{curR.toFixed(2)}R</b> / 2.0R</>}
          {phase === 'free' && (
            <>
              <em className="half-tag">½</em>
              <b>+{curR.toFixed(2)}R</b>
              <span className="dd">回撤 {drawdownR.toFixed(2)}/0.80R</span>
            </>
          )}
        </span>
        <span className="c4-mid">峰+{mfePct.toFixed(1)} 谷{(p.mae_pct || 0).toFixed(1)}</span>
        <span className="c4-time">{ageH.toFixed(1)}h/{maxH.toFixed(0)}h</span>
      </div>
    </article>
  )
}

function EmptyCard({ avatar }) {
  return (
    <article className="card idle">
      {/* 行1：与正常卡一致 —— 专属图标 + 名字位 + 标签 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <img className="av" src={avatar || avatars[avatars.length - 1]} alt="" style={{ opacity: .45 }} />
        <b style={{ fontSize: 15, letterSpacing: '0.05em', whiteSpace: 'nowrap' }}>待进场</b>
        <em className="tg" style={{
          color: 'rgba(236,79,174,.5)',
          borderColor: 'rgba(236,79,174,.28)',
        }}>IDLE</em>
      </div>

      {/* 正中间：大字 + 动态点点 */}
      <div className="idle-mid">
        <span className="idle-big">
          等待信号<i className="idot">。</i><i className="idot">。</i><i className="idot">。</i>
        </span>
      </div>
    </article>
  )
}

export default function SpiderBoard({ status, positions = [], nav, activeNav, onNavigate, children }) {
  // 图标分配：10 个持仓 ⇄ 10 个不同头像
  const avatarMap = buildAvatarMap(positions || [])
  const avatarMapSize = Object.keys(avatarMap).length

  const [ref, s] = useFitScale()
  const [now, setNow] = useState(new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const dt = status?.daily_target || {}
  const pnl = Number(dt.pnl || 0)
  const target = Number(dt.target || 200) || 1
  const pct = Math.max(0, Math.min(200, (pnl / target) * 100))

  return (
    <div className="spider-root" ref={ref} style={{ height: '100vh' }}>
      <div className="wrap">
        <div className="fit">
          <div className="board" style={{ '--s': s }}>

            {/* ── 标题轨（照参考网页：logo + 34px 标题 + 斜纹进度条 + 时钟）── */}
            <header className="hdr">
              <div className="brand">
                <span className="logo" aria-hidden="true">
                  <svg viewBox="0 0 32 32">
                    <g fill="none" stroke="currentColor" strokeWidth="2"
                       strokeLinecap="round" strokeLinejoin="round">
                      <path d="M13 13 8 7 5 2M12 15 5 12 1 13M12 17 5 19 1 23M13 19 9 25 6 30M19 13l5-6 3-5M20 15l7-3 4 1M20 17l7 2 4 4M19 19l4 6 3 5" />
                    </g>
                    <ellipse cx="16" cy="16" rx="4.6" ry="6" fill="currentColor" />
                  </svg>
                </span>
                <h1>AGENT OPS<em>+</em><span className="zh">控制台</span></h1>
                <div className="tagline">
                  TREND FOLLOWS · RISK FIRST · AGENTS EXECUTE
                  <small>实时策略 · {positions.length} 个持仓 · {status?.monitor_n ?? 0} 个监控标的</small>
                </div>
              </div>

              <div className="spacer" style={{ flex: 1 }} />

              {/* 运行指示灯：运行中绿点 / 停盘红点 */}
              <span className="runled">
                <i style={{
                  background: status ? '#35e08a' : '#ff4d5e',
                  boxShadow: status ? '0 0 8px #35e08a' : '0 0 8px #ff4d5e',
                }} />
                {status ? '运行中' : '停盘'}
              </span>

              <div className="goal">
                <span className="gl">今日盈亏<small>{pnl >= 0 ? '+' : ''}${pnl.toFixed(0)} / {target.toFixed(0)}</small></span>
                <span className="track"><i style={{ width: `${Math.min(100, pct)}%` }} /></span>
                <span className="gpct">{pct.toFixed(0)}%</span>
              </div>

              <div className="day">
                {now.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}
              </div>

              <div className="btns">
                {nav && nav.map(([k, label]) => (
                  <button key={k} className="btn" onClick={() => onNavigate(k)}
                          style={activeNav === k ? {
                            background: 'rgba(27,6,19,.85)', color: '#ff7fcb',
                            borderColor: '#1b0613',
                          } : {}}>
                    {label}
                  </button>
                ))}
              </div>

              <ThemeSwitch />

              <div className="clock">{now.toLocaleTimeString('zh-CN', { hour12: false })}</div>
            </header>

            {/* ── 14 格持仓卡 ── */}
            <section className="cards">
              {positions.slice(0, 10).map((p) => (
                <PosCard key={p.id} p={p} avatar={avatarMap[(p.symbol || '').replace('USDT', '')]} />
              ))}
              {Array.from({ length: Math.max(0, 10 - positions.length) }).map((_, i) => (
                <EmptyCard key={'e' + i} avatar={emptyAvatar(i, avatarMapSize)} />
              ))}
            </section>

            {children}
          </div>
        </div>
      </div>
    </div>
  )
}
