import ThemeSwitch from '../ThemeSwitch'
import { useEffect, useRef, useState } from 'react'
import '../spider.css'
import avatars from '../avatars.json'
import { coinColor } from '../coinColors'

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

  /* ── 出场进度（2026-11-10 B 档版）────────────────────────────────
     一条刻度尺 + 三层叠加标记，三种状态共用同一套坐标：

       轴下限 axisLo = 亏损时 -1R（止损），盈利时 0（开仓）
       轴上限 axisHi = 亏损时 峰顶（往右延伸），盈利时 max(兜底线, 峰顶)

     标记层（从下往上）：
       ① 主填充   已走完的路（亏损=已亏 / 冲刺=距兜底线 / 平半后=红满+橙黄）
       ② 回撤区   当前价 → 峰顶，虚点像素填充（真实数据：曾赚多少、现回吐多少）
       ③ 游标     当前价（青）+ 峰顶（白）

     轴上只画「价位触发」的三条：硬止损 -1R / 平半 half_at_r / 兜底全平 full_at_r，
     数值一律从后端读。右侧止盈是 K 线形态触发，没有「多少 R 就出场」这回事，
     所以轴上不画线，只在第 4 行图例里说明（½ / 2.0 / 侧 三盏灯，只有前两盏有真数据）。
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

  // ── B 档出场刻度（2026-11-10 重构）──
  // 现役出场只有四条：① 硬止损 ② 1.2R 平半 ③ 2.0R 兜底全平 ④ 右侧止盈。
  // 前三条是价位触发 → 直接画在 R 轴上（刻度值从后端读，前端不写死）；
  // ④ 是 K 线形态触发（反向关键K线 + HA 收盘穿回 ZLSMA50），策略在 0.5s 循环里
  //   现算现用、不落库，所以不画灯，只给规则图例。
  // ⚠ 老的「镜像出场三闸 + 快速失败第四盏灯」整块删除：这两套规则已下线，
  //   留着灯会让人以为仓位还受它们管（比不画更糟）。
  const halfR = Number(p.half_at_r) || 0
  const fullR = Number(p.full_at_r) || 2
  const halfDone = !!p.half_closed
  const phase = curR < 0 ? 'loss' : (halfDone || curR >= fullR) ? 'free' : 'run'
  const stopPct = (px && sl) ? Math.abs(px - sl) / px * 100 : 0
  const drawdownR = Math.max(0, peakR - curR)               // 距峰顶回撤了多少 R
  const stopNear = phase === 'loss' && Math.abs(curR) >= 0.7

  // ── 统一刻度 ──
  // 亏损态左端 = 止损（-1R），盈利态左端 = 开仓（0R）。
  // 2026-11-08：镜像出场没有「回撤出局线」，轴下限回到这两个固定端点。
  // 亏损超过 1R 时再把轴让到当前价，保证游标不会被 clamp 在左端看不见。
  const axisLo = phase === 'loss' ? Math.min(-1, curR) : 0
  const axisHi = phase === 'loss'
    ? Math.max(0.05, peakR)          // 亏损：右端延伸到峰顶（先赚后亏也要画得出来）
    : Math.max(fullR, peakR)         // 盈利：右端到兜底线或峰顶（防止冲高回落时越界）
  const axisSpan = Math.max(0.01, axisHi - axisLo)
  const pctOf = (r) => Math.max(0, Math.min(100, ((r - axisLo) / axisSpan) * 100))

  const curPct = pctOf(curR)
  const peakPct = pctOf(peakR)
  const zeroPct = pctOf(0)                                 // 开仓位置
  const twoPct = pctOf(fullR)                              // 兜底全平位置
  const showPeak = peakPct - curPct > 0.4                  // 回撤区太窄就不画了

  const ageH = (p.age_sec || 0) / 3600

  const retPct = entry ? ((long ? px - entry : entry - px) / entry) * 100 : 0

  return (
    <article className="card work" style={{ '--c': coinColor(sym) }}>
      {/* 行1：头像 + 币种 + 方向标签 + 浮盈 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <img className="av" src={avatar} alt="" />
        <b className="k">{sym}</b>
        <em className={`tg tg-${long ? 'long' : 'short'}`}>{long ? '多' : '空'}</em>
        <span key={Math.round(upnl * 100)} className="pc pnl-num" style={{
          color: win ? 'var(--red, #ff4d5e)' : 'var(--green, #35e08a)',
        }}>
          {win ? '+' : ''}{upnl.toFixed(2)}
        </span>
      </div>

      {/* 行2：开仓价 → 现价 + 收益率 */}
      <div className="row2" style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        fontVariantNumeric: 'tabular-nums',
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
      <div className={`bar prog ${phase}${stopNear ? ' near' : ''}`}>
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

        {/* ③ 峰顶：轨道上方绿色倒三角 */}
        {showPeak && <span className="peak-arrow" style={{ left: `${Math.min(Math.max(peakPct, 1.6), 98.4)}%` }} />}

        {/* ④ 当前价：轨道下方橙黄正三角，尖端压进轨道 */}
        <b className="cur-arrow" style={{ left: `${Math.min(Math.max(curPct, 1.6), 98.4)}%` }} />
        {phase === 'loss' && <span className="stop-anchor" />}
      </div>

      {/* 行4：状态数字（主位）+ 出场条件 + 持仓时间 */}
      <div className="c4">
        <span className={`prog-txt ${phase}${stopNear ? ' danger' : ''}`}>
          {phase === 'loss' && <>距止损 <b>{stopPct.toFixed(1)}%</b></>}
          {phase === 'run' && <><b>+{curR.toFixed(2)}R</b> / {fullR.toFixed(1)}R</>}
          {phase === 'free' && (
            <>
              <em className="half-tag">½</em>
              <b>+{curR.toFixed(2)}R</b>
            </>
          )}
        </span>
        {/* B 档出场图例：三个灯只画「有真数据」的部分 ——
            ½ = 已平半（DB 里 half_closed，真值）
            2.0 = 已到兜底线（用当前 R 现算，真值）
            侧 = 右侧止盈（形态触发、不落库，只作图例，永远不亮） */}
        <span className="c4-exit"
              title={`现役出场四条：硬止损 / ${halfR.toFixed(1)}R 平半 / ${fullR.toFixed(1)}R 兜底全平 / 右侧止盈`
                + `（平半之后，出现反向关键K线且 HA 收盘穿回 ZLSMA50 → 平掉剩余）`
                + `｜峰顶 +${peakR.toFixed(2)}R，现回撤 ${drawdownR.toFixed(2)}R`}>
          <b>出场</b>
          <span className={`ck${halfDone ? ' on' : ''}`}
                title={halfDone
                  ? `已在 ${halfR.toFixed(1)}R 平掉一半，剩余半仓交给右侧止盈`
                  : `到 ${halfR.toFixed(1)}R 平掉一半（锁定不亏）`}>
            <i className="led" />½
          </span>
          <span className={`ck${curR >= fullR - 1e-9 ? ' on' : ''}`}
                title={`到 ${fullR.toFixed(1)}R 兜底全平（防止一直不穿均线）`}>
            <i className="led" />{fullR.toFixed(1)}
          </span>
          <span className="ck"
                title="右侧止盈：平半之后，出现反向关键K线且 HA 收盘穿回 ZLSMA50 → 平掉剩余（形态触发，不落库）">
            <i className="led" />侧
          </span>
        </span>
        <span className="c4-time" title="时间止损已删除（B 档），这里只看已持有多少小时">已持 {ageH.toFixed(1)}h</span>
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
        <b className="k">待进场</b>
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
                  <button key={k} className={'btn' + (activeNav === k ? ' on' : '')}
                          aria-pressed={activeNav === k}
                          onClick={() => onNavigate(k)}>
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
