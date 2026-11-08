/**
 * 网络健康告警浮窗（2026-11-08 浩哥定）。
 *
 * 为什么这个组件能弹出：前端轮询的是本地 FastAPI（127.0.0.1:8899），
 * 它只读本地 SQLite，不碰币安。网络 / 代理挂了，只有「后端→币安」那一段断，
 * 「浏览器→本地后端」这一段照常通 —— 所以弹窗一定能出来。
 *
 * 样式按浩哥偏好：无遮罩、浮在界面上的小窗、深色毛玻璃、贴右上角。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

const POLL_MS = 10000   // 10 秒一轮，够快又不打库

export default function NetAlertBanner() {
  const [alert, setAlert] = useState(null)
  const [netDown, setNetDown] = useState(false)

  const load = useCallback(() => {
    api.systemAlerts(5, true)
      .then((d) => {
        const rows = d?.rows || []
        // 只弹 critical（网络中断）；恢复通知是 info，不打断
        const critical = rows.find((r) => r.severity === 'critical' && !r.acked)
        setAlert(critical || null)
        setNetDown((d?.net?.status || 'ok') === 'down')
      })
      .catch(() => {})   // 后端都连不上就不弹（那种情况是 Hermes 自己的事）
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const ack = (id) => {
    api.ackSystemAlert(id).then(load).catch(() => {})
  }

  // ── 没弹窗但网络还是断的 → 收成一条常驻小红条 ──
  // 为什么必须有：点了「知道了」只是消掉弹窗，不代表网络好了。如果这时候
  // 屏幕上什么都没了，人会以为没事了 —— 而底线是「持仓无人看管」。
  if (!alert) {
    if (!netDown) return null
    return (
      <div className="netalert-wrap" role="status">
        <div className="netalert-mini">
          <span className="netalert-dot" />
          <span>网络仍未恢复 · 出场判断失效</span>
          <button
            type="button"
            className="netalert-mini-btn"
            onClick={() => { setNetDown(false); load() }}
          >详情</button>
        </div>
      </div>
    )
  }

  const time = alert.ts
    ? new Date(alert.ts * 1000).toLocaleString('zh-CN', { hour12: false })
    : '--'

  return (
    <div className="netalert-wrap" role="alert">
      <div className="netalert">
        <div className="netalert-head">
          <span className="netalert-dot" />
          <span className="netalert-title">{alert.message || '交易系统网络中断'}</span>
        </div>
        <div className="netalert-body">
          <div className="netalert-row">
            <span className="netalert-k">发生时间</span>
            <span className="netalert-v">{time}</span>
          </div>
          {alert.detail ? (
            <div className="netalert-row">
              <span className="netalert-k">失败原因</span>
              <span className="netalert-v netalert-err">{alert.detail}</span>
            </div>
          ) : null}
          <div className="netalert-row">
            <span className="netalert-k">影响</span>
            <span className="netalert-v">
              出场判断已失效，持仓无人看管
            </span>
          </div>
          <div className="netalert-tip">
            请检查：网络连接 / 代理 7897 是否在跑 / 币安是否封了出口 IP。
            系统仍在每 10 秒自检，恢复后会自动通知。
          </div>
        </div>
        <div className="netalert-foot">
          <button
            type="button"
            className="netalert-btn"
            onClick={() => ack(alert.id)}
          >
            知道了
          </button>
        </div>
      </div>
    </div>
  )
}
