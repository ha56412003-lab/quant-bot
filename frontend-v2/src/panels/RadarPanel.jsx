import React from 'react'
import { coinColor } from '../coinColors'

/**
 * 持仓强弱雷达面板
 *
 * 位置：持仓操作区下边（原来「净值 / 浮动盈亏 / 今日已实现」三项的位置）
 * 视觉：照 agent-display #spider 的 LIVE FEED 任务流模块
 *       —— 标题行（大写字 + 中文副标 + 右侧统计）+ 等宽小字日志行
 * 数据：/api/radar，后端每分钟扫一次持仓，只提示不干预交易
 *
 * 行的读法（从左到右）：
 *   时间 · 币种 · 档位 · 方向 · 浮盈R · 分数+趋势 · 强弱条 · 动作
 *
 * 排序：分数低的排在上 —— 最危险的先看到，不用往下翻
 */
const LV = (s) => (s >= 80 ? 'lv4' : s >= 60 ? 'lv3' : s >= 40 ? 'lv2' : 'lv1')

// 档位 -> 一个动作。完整建议挂在 title 里，鼠标悬停能看到。
const ACT = {
  走势很强: '持有',
  走势放缓: '盯紧',
  走势转弱: '观望',
  中性偏弱: '观望',
  走势力竭: '离场',
}

export default function RadarPanel({ rows = [] }) {
  const list = [...(rows || [])].sort((a, b) => (a.score ?? 0) - (b.score ?? 0))
  const nDead = list.filter((r) => (r.score ?? 0) < 40).length
  const nWeak = list.filter((r) => (r.score ?? 0) >= 40 && (r.score ?? 0) < 60).length

  const hhmm = (t) => {
    if (!Number(t)) return '--:--'
    const d = new Date(Number(t) * 1000)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }

  return (
    <article className={'rp' + (list.length ? '' : ' rp-wide')}>
      <div className="rp-h">
        <h2>
          RADAR<i>持仓强弱雷达</i>
        </h2>
        <span className={'rp-m' + (list.length ? '' : ' rp-m-idle')}>
          {list.length
            ? `${list.length} 持仓 · ${nDead} 力竭 · ${nWeak} 转弱`
            : '空仓 · 雷达待命'}
        </span>
      </div>

      <ol className="rp-f">
        {list.length === 0 && (
          <li className="rp-idle">
            <svg className="rp-scan" viewBox="0 0 200 200" aria-hidden="true">
              <defs>
                <linearGradient id="rpTrail" x1="0" y1="0" x2="1" y2="0">
                  <stop offset="0%" stopColor="rgba(255,63,142,0)" />
                  <stop offset="100%" stopColor="rgba(255,63,142,.55)" />
                </linearGradient>
              </defs>
              <circle cx="100" cy="100" r="92" />
              <circle cx="100" cy="100" r="64" />
              <circle cx="100" cy="100" r="36" />
              <circle cx="100" cy="100" r="2.5" className="rp-dot" />
              <line x1="100" y1="8" x2="100" y2="192" />
              <line x1="8" y1="100" x2="192" y2="100" />
              <g className="rp-sweep-line">
                <path d="M100,100 L100,8 A92,92 0 0,1 178,38 Z" fill="url(#rpTrail)" opacity=".5" />
                <line x1="100" y1="100" x2="100" y2="8" className="rp-sweep-arm" />
              </g>
            </svg>
            <span className="rp-idle-t">没有持仓 · 雷达待命</span>
            <span className="rp-idle-s">有仓时每分钟扫一次，只提示，不改系统的单</span>
          </li>
        )}
        {list.map((r) => {
          const sc = Math.max(0, Math.min(100, Number(r.score) || 0))
          const lv = LV(sc)
          const prev = r.history && r.history[1] ? Number(r.history[1].score) : null
          const arrow = prev == null ? '' : sc > prev + 2 ? '↑' : sc < prev - 2 ? '↓' : '→'
          const rn = Number(r.r_now) || 0
          const name = (r.symbol || '').replace('USDT', '')
          return (
            <li key={r.symbol + r.side} title={r.advice || ''}>
              <time>{hhmm(r.ts)}</time>
              <b style={{ color: coinColor(name) }}>{name}</b>
              <em className={lv}>{(r.verdict || '—').slice(-2)}</em>
              <span className="rp-r">
                <i className={r.side === 'long' ? 'rp-dir c-red' : 'rp-dir c-green'}>
                  {r.side === 'long' ? '多' : '空'}
                </i>
                {' '}{rn >= 0 ? '+' : ''}{rn.toFixed(2)}R
              </span>
              <span className={'rp-sc ' + lv}>
                {Math.round(sc)}{arrow}
              </span>
              <span className={'rp-act ' + lv}>{ACT[r.verdict] || '—'}</span>
            </li>
          )
        })}
      </ol>

      {list.length > 0 && (
      <div className="rp-foot">
        <span className="rp-lg">
          <i className="lg4" />很强
          <i className="lg3" />放缓
          <i className="lg2" />转弱
          <i className="lg1" />力竭
        </span>
        <span className="rp-note">每分钟扫一次 · 只提示，不改系统的单</span>
      </div>
      )}
    </article>
  )
}
