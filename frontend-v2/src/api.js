const get = async (path) => {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}

const post = async (path, body) => {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) {
    // FastAPI 的 HTTPException 可能是 {detail:"..."}，也可能是 {detail:{error,fix}}
    const det = d.detail
    let msg = `${path}: ${r.status}`
    if (typeof det === 'string') msg = det
    else if (det && typeof det === 'object') {
      msg = det.error || JSON.stringify(det)
      if (det.fix) msg += ` — ${det.fix}`
    }
    const err = new Error(msg)
    err.detail = det
    throw err
  }
  // ⚠ 后端有「HTTP 200 + {ok:false}」的返回（平仓失败、一键全平部分失败、
  //   仓位已被别处平掉……）。只查 r.ok 会把这些当成功 → 页面弹「已平仓」
  //   其实没平，风险敞口和认知彻底脱节。
  if (d && d.ok === false) {
    const err = new Error(d.error || d.detail || '操作失败')
    err.payload = d          // 调用方可读 failed / positions / fix 这些字段
    throw err
  }
  return d
}

export { post }

export const api = {
  post,
  get,
  status: () => get('/api/status'),
  positions: () => get('/api/positions'),
  pool: () => get('/api/pool'),
  shadow: () => get('/api/shadow'),
  radar: () => get('/api/radar'),
  trades: (limit = 100) => get(`/api/trades?limit=${limit}`),
  signals: (limit = 50) => get(`/api/signals?limit=${limit}`),
  gates: () => get('/api/gates'),
  events: () => get('/api/events'),
  factors: () => get('/api/factors'),
  strategies: () => get('/api/strategies'),
  toggleStrategy: (id, enabled) => post(`/api/strategies/${id}/toggle`, { enabled }),
  strategyConfig: (id, payload) => post(`/api/strategies/${id}/config`, payload),
  createStrategy: (base, name) => post('/api/strategies/create', { base, name }),
  deleteStrategy: async (id) => {
    const r = await fetch(`/api/strategies/${id}`, { method: 'DELETE' })
    const d = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(d.detail || `${r.status}`)
    return d
  },
  journal: (q = {}) => get(`/api/journal?${new URLSearchParams(q)}`),
  risk: () => get('/api/risk'),
  // 网络健康告警（2026-11-08）
  systemAlerts: (limit = 20, onlyUnacked = false) =>
    get(`/api/system-alerts?limit=${limit}&only_unacked=${onlyUnacked}`),
  ackSystemAlert: (id) => post(`/api/system-alerts/${id}/ack`, {}),
  netStatus: () => get('/api/netstatus'),
  setGlobalRisk: (payload) => post('/api/risk/global', payload),
  universe: () => get('/api/universe'),
  // 三层池：候选池（每天 100 只）
  candidates: () => get('/api/pool/candidates'),
  rebuildCandidates: () => post('/api/pool/candidates/rebuild', {}),
  setWhitelist: (symbols) => post('/api/pool/whitelist', { symbols }),
  credentials: () => get('/api/credentials'),
  setCredentials: (payload) => post('/api/credentials', payload),
  testCredentials: (which) => post('/api/credentials/test', { which }),
  setMode: (mode, confirm) => post('/api/mode', { mode, confirm }),
  livePrecheck: () => get('/api/live/precheck'),
  klines: (symbol, interval = '15m') => get(`/api/klines?symbol=${symbol}&interval=${interval}`),
  logs: (limit = 100) => get(`/api/logs?limit=${limit}`),
}

export const fmtUsd = (v, digits = 2) =>
  v == null ? '-' : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: digits })}`

export const fmtPnl = (v) =>
  v == null ? '-' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}`

export const fmtDur = (sec) => {
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}
