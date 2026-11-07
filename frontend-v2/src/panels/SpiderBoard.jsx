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
  /* ── 出场进度（2026-11-06 第三版）────────────────────────────────
     一条刻度尺 + 四层叠加标记，三种状态共用同一套坐标：

       轴下限 axisLo = 亏损时 -1R（止损），盈利时 0（开仓）
       轴上限 axisHi = 亏损时 峰顶（往右延伸），盈利时 max(2R, 峰顶)

     标记层（从下往上）：
       ① 主填充   已走完的路（亏损=已亏 / 冲刺=距2R / 平半后=红满+橙黄）
       ② 回撤区   当前价 → 峰顶，虚点像素填充
       ③ 出局线   峰顶往左「当前回撤门槛」，跌破即出场（未满 2R = 浮盈50%，满 2R = 0.8R）
       ④ 游标     当前价（青）+ 峰顶（白）
  */
  const risk = (entry && sl) ? Math.abs(entry - sl) : 0     // 1R 的价格距离
  const gain = (entry && px) ? (long ? px - entry : entry - px) : 0
  const curR = risk > 0 ? gain / risk : 0

  // 峰顶 R：内存态和后端 mfe_pct 反推取【最大】。
  // 两个来源更新频率不同（engine 每轮刷 mfe_pct，check_exit 才刷 peak），
  // 单取任一个都会让峰顶偏低 → 回撤区画小 → 出局线位置骗人。
  const mfePct = Math.abs(p.mfe_pct || 0)
  const peakFromMfe = (risk > 0 && entry) ? (mfePct / 100 * entry) / risk : 0
  const peakFromMem = Number(p.peak_r_mem) || 0
  const peakR = Math.max(peakFromMfe, peakFromMem, curR)

  const halfDone = !!p.half_closed
  const phase = curR < 0 ? 'loss' : (halfDone || curR >= 2) ? 'free' : 'run'
  const stopPct = (px && sl) ? Math.abs(px - sl) / px * 100 : 0
  const drawdownR = Math.max(0, peakR - curR)               // 距峰顶回撤了多少 R
  // 当前要求的回撤门槛：峰顶不足 2R 时是「浮盈的 50%」（后端算好），满 2R 后是 0.8R。
  // 前端只负责显示，不再硬编码 0.8。
  const trailTarget = Math.max(0.01, Number(p.trail_target_r) || 0.8)
  const ddNear = drawdownR >= trailTarget * 0.75           // 逼近触发线
  const stopNear = phase === 'loss' && Math.abs(curR) >= 0.7

  // ── 统一刻度 ──
  // 出局线的位置 = 峰顶 - 当前回撤门槛。峰顶不足门槛时这条线会落在开仓价左边（负 R），
  // 但它【依然存在】—— 意即「跌破这里就该走」，不是「不存在」。
  // 所以轴下限必须让到这条线，否则它会画到轨道外面、根本没渲染。
  const exitR = peakR - trailTarget
  const axisLo = phase === 'loss'
    ? Math.min(-1, exitR)            // 亏损态：左端是止损（-1R），出局线若更左就再让
    : Math.min(0, exitR)             // 盈利态：左端是开仓（0R），出局线更左就让到那
  const axisHi = phase === 'loss'
    ? Math.max(0.05, peakR)          // 亏损：右端延伸到峰顶（先赚后亏也要画得出来）
    : Math.max(2, peakR)             // 盈利：右端到 2R 或峰顶（防止冲到 2.3R 后回落时越界）
  const axisSpan = Math.max(0.01, axisHi - axisLo)
  const pctOf = (r) => Math.max(0, Math.min(100, ((r - axisLo) / axisSpan) * 100))

  const curPct = pctOf(curR)
  const peakPct = pctOf(peakR)
  const zeroPct = pctOf(0)                                 // 开仓位置
  const twoPct = pctOf(2)                                  // 2R 位置
  const exitPct = pctOf(exitR)                             // 回撤出局线
  const showExit = exitR > axisLo - 1e-9 && exitR < axisHi // 只要落在轴内就画
  const showPeak = peakPct - curPct > 0.4                  // 回撤区太窄就不画了

  const maCnt = Number(p.ma_break_cnt) || 0
  const maReq = Math.max(1, Number(p.ma_confirm_bars) || 2)

  const ageH = (p.age_sec || 0) / 3600
  const maxH = (p.max_hold_sec || 1) / 3600
  const retPct = entry ? ((long ? px - entry : entry - px) / entry) * 100 : 0

  return (
    <article className="card work" style={{ '--c': win ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)' }}>
      {/* 行1：头像 + 币种 + 方向标签 + 浮盈 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <img className="av" src={avatar} alt="" />
        <b style={{ fontSize: 15, letterSpacing: '0.05em', whiteSpace: 'nowrap' }}>{sym}</b>
        <em className="tg" style={{
          color: long ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)',
          borderColor: long ? 'rgba(255,77,94,.55)' : 'rgba(53,224,138,.55)',
        }}>{long ? 'LONG' : 'SHORT'}</em>
        <span key={Math.round(upnl * 100)} className="pnl-num" style={{
          marginLeft: 'auto', fontWeight: 900,
          fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
          color: win ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)',
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
          color: win ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)',
        }}>
          {retPct >= 0 ? '+' : ''}{retPct.toFixed(2)}%
        </span>
      </div>

      {/* 行3：出场进度 —— 统一 R 轴 + 四层标记 */}
      <div className={`bar prog ${phase}${stopNear || ddNear ? ' near' : ''}`}>
        {/* ① 主填充 */}
        {phase === 'loss' && (
          <i style={{ left: `${curPct}%`, width: `${Math.max(0, zeroPct - curPct)}%` }} />
        )}
        {phase === 'run' && (
          <i style={{ left: `${zeroPct}%`, width: `${Math.max(0, curPct - zeroPct)}%` }} />
        )}
        {phase === 'free' && (
          <>
            {/* 红条：0 → 2R 打底，表示主仓已经达成平半目标 */}
            <i className="base2r" style={{ left: `${zeroPct}%`, width: `${twoPct - zeroPct}%` }} />
            {/* 橙黄层：从 0 起手，整段【压在红条之上】——
                半透明，底下红条透得出来，形成"红底 + 橙黄覆盖"的层次，
                末端继续向右延伸出 2R 之外，就是半仓还在跑的距离。 */}
            <i className="halflayer" style={{
              left: `${zeroPct}%`,
              width: `${Math.max(0, curPct - zeroPct)}%`,
            }} />
          </>
        )}

        {/* ② 羽化回撤区：当前价 → 峰顶，边缘渐隐的流动光带 */}
        {showPeak && (
          <span className="dd-zone" style={{
            left: `${curPct}%`, width: `${peakPct - curPct}%`,
          }} />
        )}

        {/* ③ 出局标记：红色倒三角（闪烁）+ 两个小圆点
              亮一个 = 已经触碰过一次出局线；亮两个 = 该出场了
              峰顶不足门槛时这条线落在负 R 区（意味着认亏离场），同样要画出来 */}
        {showExit && (
          <span className="exit-mark" style={{ left: `${Math.max(exitPct, 1.6)}%` }}>
            <i className="tri" />
            {Array.from({ length: maReq }, (_, k) => (
              <i key={k} className={`dot${maCnt > k ? ' on' : ''}`} />
            ))}
          </span>
        )}

        {/* 破均线计数点：独立于出局线。峰顶还没到门槛时出局线落在负 R 区，
            但「已经连续几根破均线」这个信息照样要能看见 ——
            贴在峰顶箭头右边，不抢出局标记的位置 */}
        {!showExit && maCnt > 0 && (
          <span className={`ma-dots${peakPct > 88 ? ' flip' : ''}`} style={{ left: `${Math.min(peakPct, 100)}%` }}>
            {Array.from({ length: maReq }, (_, k) => (
              <i key={k} className={`dot${maCnt > k ? ' on' : ''}`} />
            ))}
          </span>
        )}

        {/* ④ 峰顶：轨道上方绿色倒三角 */}
        {showPeak && <span className="peak-arrow" style={{ left: `${Math.min(Math.max(peakPct, 1.6), 98.4)}%` }} />}

        {/* ④ 当前价：轨道下方橙黄正三角，尖端压进轨道 */}
        <b className="cur-arrow" style={{ left: `${Math.min(Math.max(curPct, 1.6), 98.4)}%` }} />
        {phase === 'loss' && <span className="stop-anchor" />}
      </div>

      {/* 行4：状态数字（主位）+ 出场条件 + 持仓时间 */}
      <div className="c4">
        <span className={`prog-txt ${phase}${stopNear || ddNear ? ' danger' : ''}`}>
          {phase === 'loss' && <>距止损 <b>{stopPct.toFixed(1)}%</b></>}
          {phase === 'run' && <><b>+{curR.toFixed(2)}R</b> / 2.0R</>}
          {phase === 'free' && (
            <>
              <em className="half-tag">½</em>
              <b>+{curR.toFixed(2)}R</b>
            </>
          )}
        </span>
        <span className={`c4-mid${maCnt >= maReq ? ' hot' : ''}`}
              title={`连续 ${maCnt} 根收盘破 ZLSMA50（需 ${maReq} 根）+ 从峰顶回撤 ${drawdownR.toFixed(2)}R（需 ${trailTarget.toFixed(2)}R${p.trail_is_pct ? '，按浮盈50%算' : ''}）`}>
          破均线 {Math.min(maCnt, maReq)}/{maReq} · 回撤 {drawdownR.toFixed(2)}/{trailTarget.toFixed(2)}R
        </span>
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
                  background: status ? 'var(--green, #35e08a)' : 'var(--red, #ff4d5e)',
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
