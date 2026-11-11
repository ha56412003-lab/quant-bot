import { useEffect, useState } from 'react'
import {
  Card, Row, Col, Input, Button, message, Descriptions, Tag, Space,
  Select, Alert, Radio, Table, Switch,
} from 'antd'
import { api, fmtUsd } from '../api'
import { useI18n } from '../i18n'

const CRED_FIELDS = [
  { key: 'clawby_api_key', label: 'Clawby API Key', span: 24 },
  { key: 'binance_api_key', label: 'Binance API Key', span: 12 },
  { key: 'binance_secret_key', label: 'Binance Secret Key', span: 12 },
  { key: 'bitget_api_key', label: 'Bitget API Key', span: 8 },
  { key: 'bitget_secret_key', label: 'Bitget Secret Key', span: 8 },
  { key: 'bitget_passphrase', label: 'Bitget Passphrase', span: 8 },
  { key: 'okx_api_key', label: 'OKX API Key', span: 8 },
  { key: 'okx_secret_key', label: 'OKX Secret Key', span: 8 },
  { key: 'okx_passphrase', label: 'OKX Passphrase', span: 8 },
]

function Credentials() {
  const { t } = useI18n()
  const [cur, setCur] = useState(null)
  const [form, setForm] = useState({})
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState(null)

  const load = () => api.credentials().then(setCur).catch(() => {})
  useEffect(() => { load() }, [])

  const save = async () => {
    const payload = {}
    for (const f of CRED_FIELDS) if (form[f.key]) payload[f.key] = form[f.key].trim()
    if (!Object.keys(payload).length) return message.info(t('config.nothingToSave'))
    try {
      await api.setCredentials(payload)
      message.success(t('config.credsSaved'))
      setForm({}); load()
    } catch (e) { message.error(`${t('config.saveFailed')}: ${e.message}`) }
  }

  const runTest = async () => {
    setTesting(true); setTest(null)
    try { setTest(await api.testCredentials('both')) }
    catch (e) { message.error(`${t('config.testFailed')}: ${e.message}`) }
    setTesting(false)
  }

  const badge = (ok, masked) => (ok
    ? <Tag color="green">{masked}</Tag> : <Tag>{t('config.notSet')}</Tag>)

  const venueResult = (name, r) => r && (
    <div style={{ fontSize: 12, marginBottom: 4 }}>
      {name}: {r.ok
        ? (
          <Tag color="green">
            {t('config.connOk')}
            {r.plan != null && ` · plan=${r.plan} · ${t('config.balance')}${r.balance}`}
            {r.can_trade != null && ` · ${t('config.canTrade')}=${String(r.can_trade)}`}
            {r.wallet_usdt != null && ` · ${t('config.wallet')}${fmtUsd(Number(r.wallet_usdt))}`}
            {r.available != null && ` · ${t('config.avail')}${fmtUsd(Number(r.available))}`}
          </Tag>
        )
        : <Tag color="red">{t('config.failed')}: {r.error}</Tag>}
    </div>
  )

  return (
    <Card size="small" title={t('config.creds')}>
      <Alert type="warning" showIcon style={{ marginBottom: 12, fontSize: 12 }}
             message={t('config.credsWarn')} />
      {cur && (
        <Descriptions size="small" column={2} bordered style={{ marginBottom: 14 }}
                      labelStyle={{ width: 140 }}>
          <Descriptions.Item label="Clawby">{badge(cur.has_clawby, cur.clawby_api_key)}</Descriptions.Item>
          <Descriptions.Item label="Binance">{badge(cur.has_binance, cur.binance_api_key)}</Descriptions.Item>
          <Descriptions.Item label="Bitget">{badge(cur.has_bitget, cur.bitget_api_key)}</Descriptions.Item>
          <Descriptions.Item label="OKX">{badge(cur.has_okx, cur.okx_api_key)}</Descriptions.Item>
        </Descriptions>
      )}
      <Row gutter={[16, 16]}>
        {CRED_FIELDS.map((f) => (
          <Col xs={24} md={f.span} key={f.key}>
            <div style={{ fontSize: 12, color: 'var(--mute, #7d8590)', marginBottom: 4 }}>{f.label}</div>
            <Input.Password placeholder={t('config.keepEmpty')} autoComplete="off"
                            value={form[f.key] || ''}
                            onChange={(e) => setForm((s) => ({ ...s, [f.key]: e.target.value }))} />
          </Col>
        ))}
      </Row>
      <Space style={{ marginTop: 14 }}>
        <Button type="primary" size="small" onClick={save}>{t('config.saveCreds')}</Button>
        <Button size="small" loading={testing} onClick={runTest}>{t('config.testConn')}</Button>
      </Space>
      {test && (
        <div style={{ marginTop: 16 }}>
          {venueResult('Clawby', test.clawby)}
          {venueResult('Binance', test.binance)}
          {venueResult('Bitget', test.bitget)}
          {venueResult('OKX', test.okx)}
        </div>
      )}
    </Card>
  )
}

function ExecutorExchange() {
  const { t } = useI18n()
  const [info, setInfo] = useState(null)

  // ★ 2026-11-11 修的 BUG：这里原本是裸 fetch('/api/executor-exchange')，
  //   不带 x-api-token → 后端 401 → 响应体 {detail:...} 被当成数据塞进 info
  //   → 下面的 info.exchange.toUpperCase() 读到 undefined → 整个配置页白屏。
  //   现在走 api.get（自动带 token / 观战 key，401 会抛错被 catch 吞掉，info 保持 null）。
  const load = () => api.get('/api/executor-exchange')
    .then(setInfo).catch(() => {})
  useEffect(() => { load() }, [])

  const switchTo = async (venue) => {
    try {
      // 走统一封装：自动带 x-api-token，只读模式下直接被拦（2026-11-11）
      await api.post('/api/executor-exchange', { exchange: venue })
      message.success(`${t('config.executorOk')}: ${venue}`)
      load()
    } catch (e) { message.error(`${t('common.opFailed')}: ${e.message}`) }
  }

  // ★ 2026-11-11：还要防「接口返回了但字段不全」（比如后端异常返回 {detail:...}），
  //   只判断 !info 拦不住，必须确认 exchange 存在才渲染，否则又是一次白屏。
  if (!info || !info.exchange) return null
  return (
    <Card size="small" title={t('config.executor')} style={{ marginTop: 16 }}>
      <Alert type="info" showIcon style={{ marginBottom: 12, fontSize: 12 }}
             message={t('config.executorHint')} />
      <Radio.Group value={info.exchange} buttonStyle="solid" size="small"
                   onChange={(e) => switchTo(e.target.value)}>
        {(info.supported || []).map((v) => (
          <Radio.Button key={v.id} value={v.id} disabled={!v.has_credentials}>
            {v.id.toUpperCase()}
            {!v.has_credentials && ` (${t('config.noCreds')})`}
          </Radio.Button>
        ))}
      </Radio.Group>
      <div style={{ marginTop: 8, fontSize: 12, color: 'var(--mute, #7d8590)' }}>
        {t('config.executorCur')}: <Tag color="green">{info.exchange.toUpperCase()}</Tag>
      </div>
    </Card>
  )
}

function UniverseConfig() {
  const { t } = useI18n()
  const [known, setKnown] = useState([])
  const [wl, setWl] = useState([])          // 白名单（自选合约）
  const [cand, setCand] = useState(null)    // 候选池快照
  const [busy, setBusy] = useState(false)

  const load = () => {
    api.candidates().then(setCand).catch(() => {})
    api.universe().then((d) => setKnown(d.known || [])).catch(() => {})
  }

  useEffect(() => {
    load()
    // 白名单从候选池元数据取（重建时会回填）
    api.candidates().then((d) => setWl(d.whitelist || [])).catch(() => {})
  }, [])

  const saveWl = async () => {
    try {
      await api.setWhitelist(wl)
      message.success('自选合约已保存，下次重建时生效')
      load()
    } catch (e) { message.error(`保存失败: ${e.message}`) }
  }

  const rebuild = async () => {
    setBusy(true)
    try {
      const r = await api.rebuildCandidates()
      message.success(`候选池重建完成：${r.size} 只`)
      load()
    } catch (e) { message.error(`重建失败: ${e.message}`) }
    finally { setBusy(false) }
  }

  const options = Array.from(new Set([...known, ...(cand?.symbols || []), ...wl]))
    .map((s) => ({ value: s, label: s }))
  const builtAt = cand?.built_at || '尚未构建'

  return (
    <Card size="small" title="每天候选币池" style={{ marginTop: 16 }}>
      <Alert type="info" showIcon style={{ marginBottom: 12, fontSize: 12 }}
        message={`每天自动重建一次。组成 = 自选合约（优先级最高）+ 按成交额排名补满 ${cand?.size ?? 200} 只。`}
        description={
          <span style={{ fontSize: 11.5, color: 'var(--mute, #7d8590)' }}>
            过滤：真加密货币 · USDT 本位 · 永续 · 在交易 · 30 天振幅 {cand?.min_amp_pct ?? 10}% ~ {cand?.max_amp_pct ?? 300}%
          </span>
        } />

      <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 6 }}>
        自选合约（白名单 · 无条件进候选池）
      </div>
      <Select mode="tags" style={{ width: '100%' }} value={wl} onChange={setWl}
              options={options} placeholder="输入币种如 DOGEUSDT，回车添加"
              tokenSeparators={[',', ' ']} />
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" size="small" onClick={saveWl}>保存白名单</Button>
        <Button size="small" loading={busy} onClick={rebuild}>立即重建候选池</Button>
      </Space>

      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid rgba(255,255,255,.06)',
                    fontSize: 12, color: '#8b949e', lineHeight: 1.9 }}>
        <div>
          <span style={{ color: 'var(--cyan, #45e6f5)' }}>当前候选池</span>：
          <b style={{ color: 'var(--ink, #e6edf3)' }}>{cand?.symbols?.length ?? 0}</b> 只
          {cand?.size ? <span style={{ color: 'var(--mute, #7d8590)' }}> / 目标 {cand.size}</span> : null}
          <span style={{ marginLeft: 12 }}>构建于 {builtAt}</span>
        </div>
        {cand?.total_tradable ? (
          <div style={{ color: 'var(--mute, #7d8590)' }}>
            全市场真币 {cand.total_tradable} 个 → 扫描 {cand.scanned} 个 → 剔除 {cand.rejected_n} 个（振幅不合格）→ 留 {cand.symbols?.length} 只
          </div>
        ) : null}
        {(cand?.whitelist_missing || []).length ? (
          <div style={{ color: '#ffa726' }}>
            ⚠ 白名单里这些币在币安不可交易，已跳过：{cand.whitelist_missing.join(', ')}
          </div>
        ) : null}
        <div style={{ marginTop: 6, color: 'var(--mute, #7d8590)', wordBreak: 'break-all' }}>
          {(cand?.symbols || []).slice(0, 40).map((s) => (
            <Tag key={s} style={{ marginBottom: 4, fontSize: 11 }}>{s.replace('USDT', '')}</Tag>
          ))}
          {(cand?.symbols || []).length > 40 ? <span>… 等 {cand.symbols.length} 只</span> : null}
        </div>
      </div>
    </Card>
  )
}

// ── 操作日志（2026-11-11）────────────────────────────────────────────
// 在加审计表之前，任何一个写接口都是「改完不留痕」：参数被谁、什么时候、
// 从哪个 IP 改成了什么，一条都查不到。这个卡片就是那条缺失的记录。
// 只有拿操作 token 的人能看（后端 /api/audit 会校验），观战者调用得 403。
function _fmtTs(ts) {
  try {
    const d = new Date((ts || 0) * 1000)
    const p = (n) => String(n).padStart(2, '0')
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  } catch (e) { return '--' }
}

// 把 detail 压成一句人话：{"changed":{"leverage":{"old":6,"new":999}}}
//                             → "leverage 6 → 999"
function _fmtDetail(d) {
  if (!d || typeof d !== 'object') return '--'
  const parts = []
  // 失败记录的形状是 {"payload": {...原始入参...}} —— 别把整坨 JSON 甩给用户，
  // 只挑出「他到底想改哪个键」。
  if (d.payload && typeof d.payload === 'object' && !d.changed) {
    const flat = []
    const walk = (o, pre) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, pre ? `${pre}.${k}` : k)
        else flat.push(`${pre ? pre + '.' : ''}${k}=${Array.isArray(v) ? `[${v.length}项]` : v}`)
      }
    }
    walk(d.payload, '')
    return `尝试写入 ${flat.slice(0, 5).join(', ')}${flat.length > 5 ? ' …' : ''}`
  }
  if (d.changed && typeof d.changed === 'object') {
    for (const [k, v] of Object.entries(d.changed)) {
      const o = v && typeof v === 'object' ? v.old : undefined
      const n = v && typeof v === 'object' ? v.new : v
      // old === new 的项没意义（比如只改 auto_close_sec，pause_new 被一起回填），跳过
      if (o !== undefined && JSON.stringify(o) === JSON.stringify(n)) continue
      parts.push(`${k} ${o === undefined || o === null ? '—' : o} → ${n}`)
    }
    if (!parts.length) parts.push('（值未变化）')
  } else if (d.keys && typeof d.keys === 'object') {
    // 凭据类：后端只给长度和前 4 位，绝不落明文
    parts.push(Object.entries(d.keys).map(([k, v]) => `${k}(${v.len}位)`).join(' '))
  } else if (d.enabled !== undefined) {
    parts.push(`enabled → ${d.enabled.new}`)
  } else if (d.count) {
    parts.push(`${d.count.old} → ${d.count.new} 个`)
    if ((d.added || []).length) parts.push(`+${d.added.slice(0, 6).join(',')}`)
    if ((d.removed || []).length) parts.push(`-${d.removed.slice(0, 6).join(',')}`)
  } else {
    parts.push(JSON.stringify(d).slice(0, 120))
  }
  return parts.join('；') || '--'
}

const ACTION_LABEL = {
  set_global_risk: '改全局风控',
  set_strategy_risk: '改策略风控',
  set_strategy_params: '改策略参数',
  set_strategy_enabled: '开关策略',
  set_strategy_symbols: '改监控币种',
  set_strategy_interval: '改扫描间隔',
  save_credentials: '改 API 凭据',
  set_universe: '改全局币池',
}

function AuditLog() {
  const { t } = useI18n()
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [onlyFail, setOnlyFail] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const r = await api.get(`/api/audit?limit=50${onlyFail ? '&only_fail=1' : ''}`)
      setRows((r && r.rows) || [])
      setTotal((r && r.total) || 0)
    } catch (e) {
      message.error(`操作日志读取失败: ${e.message || e}`)
    }
    setLoading(false)
  }
  useEffect(() => { load() }, [onlyFail])

  const cols = [
    { title: '时间', dataIndex: 'ts', width: 105, render: (v) => <span style={{ fontSize: 11 }}>{_fmtTs(v)}</span> },
    {
      title: '谁', dataIndex: 'actor', width: 90,
      render: (v) => {
        const s = String(v || '')
        const color = s.startsWith('op:') ? 'green' : (s.startsWith('view:') ? 'blue' : 'default')
        return <Tag color={color} style={{ fontSize: 10 }}>{s || '--'}</Tag>
      },
    },
    {
      title: '动作', dataIndex: 'action', width: 110,
      render: (v) => <span style={{ fontSize: 11 }}>{ACTION_LABEL[v] || v}</span>,
    },
    {
      title: '对象', dataIndex: 'target', width: 110,
      render: (v) => <span style={{ fontSize: 11, color: 'var(--mute, #7d8590)' }}>{v || '--'}</span>,
    },
    {
      title: '改了什么（旧 → 新）', dataIndex: 'detail',
      render: (v, r) => (
        <span style={{ fontSize: 11, wordBreak: 'break-all' }}>
          {_fmtDetail(v)}
          {r && r.ok ? null : <Tag color="red" style={{ marginLeft: 6, fontSize: 10 }}>失败 {r && r.err}</Tag>}
        </span>
      ),
    },
    {
      title: 'IP', dataIndex: 'ip', width: 105,
      render: (v) => <span style={{ fontSize: 10, color: 'var(--mute, #7d8590)' }}>{v || '--'}</span>,
    },
  ]

  return (
    <Card
      size="small"
      title={`操作日志（共 ${total} 条）`}
      style={{ marginTop: 16 }}
      extra={(
        <Space size={8}>
          <Switch size="small" checked={onlyFail} onChange={setOnlyFail}
                  checkedChildren="只看失败" unCheckedChildren="全部" />
          <Button size="small" onClick={load} loading={loading}>刷新</Button>
        </Space>
      )}
    >
      <div style={{ fontSize: 11, color: 'var(--mute, #7d8590)', marginBottom: 8 }}>
        记录每一次参数/凭据/币种变更：谁改的、从哪个 IP、旧值改成什么。
        API 凭据只记长度，不记内容。
      </div>
      <Table
        size="small"
        rowKey="id"
        dataSource={rows}
        columns={cols}
        loading={loading}
        pagination={rows.length > 20 ? { pageSize: 20, size: 'small' } : false}
        locale={{ emptyText: '还没有任何变更记录' }}
      />
    </Card>
  )
}

export default function ConfigPage() {
  return (
    <Row gutter={[16, 16]}>
      <Col span={24}><Credentials /></Col>
      <Col span={24}><ExecutorExchange /></Col>
      <Col span={24}><UniverseConfig /></Col>
      <Col span={24}><AuditLog /></Col>
    </Row>
  )
}
