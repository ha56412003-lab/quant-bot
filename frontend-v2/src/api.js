/* ── 写操作鉴权 / 只读观战（2026-11-11）────────────────────────────
   两种进入方式：
     1) 自己的链接：?token=xxx
        → 立刻存进 localStorage，并从地址栏抹掉（免得截图/转发把 token 漏出去）
        → 之后所有请求自动带 x-api-token，后端才放行写操作
     2) 给朋友的观战链接：?ro=1
        → READ_ONLY = true，页面隐藏所有开仓/平仓/改参数按钮
        → 而且他浏览器里没有 token，就算按 F12 手搓请求，后端也会 401
   注意：光靠前端藏按钮是假只读，真正的墙在后端中间件（backend/main.py:178）。
*/
const LS_TOKEN = 'qb_api_token'

function _initToken() {
  try {
    const q = new URLSearchParams(window.location.search)
    const t = (q.get('token') || '').trim()
    if (t) {
      localStorage.setItem(LS_TOKEN, t)
      q.delete('token')
      const qs = q.toString()
      window.history.replaceState({}, '', window.location.pathname + (qs ? '?' + qs : ''))
      return t
    }
    return (localStorage.getItem(LS_TOKEN) || '').trim()
  } catch (e) {
    return ''
  }
}

export const API_TOKEN = _initToken()

/* ── 观战 key（2026-11-11）────────────────────────────────────────
   给朋友的链接：https://quant.hahayuyue.cn/?ro=1&k=<观战key>
   → k 存进 localStorage 并从地址栏抹掉
   → 之后所有 GET 自动带 x-view-key，后端才放行读操作
   → 观战 key 只能读，不能写（写操作仍然必须有 x-api-token）
*/
const LS_VIEW = 'qb_view_key'

function _initViewKey() {
  try {
    const q = new URLSearchParams(window.location.search)
    const k = (q.get('k') || q.get('key') || '').trim()
    if (k) {
      localStorage.setItem(LS_VIEW, k)
      q.delete('k')
      q.delete('key')
      const qs = q.toString()
      window.history.replaceState({}, '', window.location.pathname + (qs ? '?' + qs : ''))
      return k
    }
    return (localStorage.getItem(LS_VIEW) || '').trim()
  } catch (e) {
    return ''
  }
}

export const VIEW_KEY = _initViewKey()

let _ro = false
try {
  const _q = new URLSearchParams(window.location.search)
  const _v = (_q.get('ro') || _q.get('readonly') || '').toLowerCase()
  _ro = ['1', 'true', 'yes', 'on'].includes(_v)
} catch (e) { _ro = false }

/** true = 只读观战模式（URL 带 ?ro=1），所有写操作在前端就被挡掉 */
export const READ_ONLY = _ro

/** 统一请求头：优先带操作 token（完整权限），没有就带观战 key（只读） */
function _hdr(extra) {
  const h = Object.assign({}, extra || {})
  if (API_TOKEN) h['x-api-token'] = API_TOKEN
  else if (VIEW_KEY) h['x-view-key'] = VIEW_KEY
  return h
}

/** 只读模式的写操作统一拒绝（第二道防线在后端） */
function _blockWrite(what) {
  const err = new Error(`只读模式（观战链接）不能${what}`)
  err.readonly = true
  return err
}

const get = async (path) => {
  const r = await fetch(path, { headers: _hdr() })
  if (!r.ok) {
    if (r.status === 401) {
      const err = new Error('没有访问权限：观战链接可能已失效，请向管理员要新链接')
      err.unauthorized = true
      throw err
    }
    if (r.status === 429) {
      const err = new Error('请求太频繁，请等几秒再刷新')
      err.rateLimited = true
      throw err
    }
    throw new Error(`${path}: ${r.status}`)
  }
  return r.json()
}

const post = async (path, body) => {
  if (READ_ONLY) throw _blockWrite('执行写操作')
  const r = await fetch(path, {
    method: 'POST',
    headers: _hdr({ 'Content-Type': 'application/json' }),
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
    if (READ_ONLY) throw _blockWrite('删除策略')
    const r = await fetch(`/api/strategies/${id}`, { method: 'DELETE', headers: _hdr() })
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
  // 手动暂停 / 恢复运营（VS15-1.1）：系统更新前先停开新仓
  opsPause: (action) => post('/api/ops/pause', { action }),
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
