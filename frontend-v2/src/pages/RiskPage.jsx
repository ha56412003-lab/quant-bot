import React from 'react'
import { useState } from 'react'
import {
  Card, Row, Col, Table, InputNumber, Button, message, Statistic,
  Tag, Alert, Switch, Divider,
} from 'antd'
import { api, fmtUsd } from '../api'
import { usePoll } from '../hooks'
import { useI18n } from '../i18n'

function GlobalRisk({ data, onSaved }) {
  const { t } = useI18n()
  const g = data?.global || {}
  // /api/risk 不含 daily_target，日目标实时状态要单独拉 /api/status
  const [dtData, dtRefresh] = usePoll(() => api.get('/api/status'), 5000)
  const dt = dtData?.daily_target || data?.daily_target || {}
  const [v, setV] = useState({})
  const [orig, setOrig] = useState({})
  /* 用户正在编辑时不能被轮询冲掉。
     /api/risk 每几秒刷新一次，data 每次都是新对象 → [data] 每次都变 →
     原来会把用户填到一半的数字直接重置回旧值。 */
  const dirty = React.useRef(false)
  React.useEffect(() => {
    if (dirty.current) return
    setV(g)
    // 记录加载时的原始值：保存时只提交**改过的**字段。
    // 否则表单里任何一个陈旧值都会覆盖掉别人刚改的参数
    // （2026-11-05 事故：max_same_side 被页面里的旧值 6 覆盖回写）
    setOrig(JSON.parse(JSON.stringify(g)))
  }, [data]) // eslint-disable-line
  const edit = (k, x) => {
    dirty.current = true
    setV((s) => ({ ...s, [k]: x }))
  }
  // 每个字段都带硬边界：填 0 / 负数 / 天文数字在这里就被挡住。
  // 之前没有任何限制 —— risk_per_trade_pct=0 会让仓位算成无穷大；
  // max_concurrent_coins=0 会直接停掉所有开仓；这些都是「一次误触就废掉系统」。
  const fields = [
    ['risk_per_trade_pct', t('risk.f.rpt'), t('risk.f.rptTip'), 0.1, 10, 0.1],
    ['max_gross_leverage', t('risk.f.lev'), t('risk.f.levTip'), 1, 20, 1],
    ['max_position_pct_per_coin', t('risk.f.coinCap'), t('risk.f.coinCapTip'), 1, 100, 1],
    ['max_concurrent_coins', t('risk.f.conc'), t('risk.f.concTip'), 1, 20, 1],
    ['max_same_side', t('risk.f.sameSide'), t('risk.f.sameSideTip'), 1, 20, 1],
    ['daily_loss_halt_pct', t('risk.f.halt'), t('risk.f.haltTip'), 1, 50, 0.5],
    ['unrealized_halt_pct', t('risk.f.uhalt'), t('risk.f.uhaltTip'), 0, 50, 0.5],
    ['event_quiet_minutes', t('risk.f.quiet'), t('risk.f.quietTip'), 0, 240, 5],
  ]
  const save = async () => {
    const diff = {}
    for (const k of Object.keys(v || {})) {
      if (JSON.stringify(v[k]) !== JSON.stringify(orig[k])) diff[k] = v[k]
    }
    if (!Object.keys(diff).length) { message.info(t('risk.noChange')); return }
    // 提交前再挡一次（InputNumber 可以被直接输入绕过 min/max）
    for (const [k, label, , lo, hi] of fields) {
      if (k in diff) {
        const n = Number(diff[k])
        if (!Number.isFinite(n) || n < lo || n > hi) {
          message.error(`${label} 必须在 ${lo} ~ ${hi} 之间（当前 ${diff[k]}）`)
          return
        }
        diff[k] = n
      }
    }
    try {
      await api.setGlobalRisk(diff)
      dirty.current = false
      message.success(t('risk.globalSaved')); onSaved?.()
    }
    catch (e) { message.error(`${t('config.saveFailed')}: ${e.message}`) }
  }
  return (
    <Card size="small" title={t('risk.global')}>
      <Row gutter={[16, 12]}>
        {fields.map(([k, label, tip, lo, hi, step]) => (
          <Col xs={12} md={8} key={k}>
            <div style={{ fontSize: 12, color: 'var(--mute, #7d8590)', marginBottom: 4 }}>{label}</div>
            <InputNumber size="small" style={{ width: '100%' }} value={v[k]}
                         min={lo} max={hi} step={step}
                         onChange={(x) => edit(k, x)} />
            <div style={{ fontSize: 11, color: '#4d5661', marginTop: 2 }}>{tip}（{lo} ~ {hi}）</div>
          </Col>
        ))}
      </Row>

      <Divider style={{ margin: '14px 0 10px', borderColor: 'rgba(193,32,126,0.2)' }} />
      <div style={{ fontSize: 12.5, color: '#9beeff', fontWeight: 700, marginBottom: 10 }}>
        {t('risk.dailyTarget')}
      </div>
      <Row gutter={[16, 12]}>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtEnable')}</div>
          <Switch checked={!!(v.daily_target || {}).enabled}
                  onChange={(c) => setV({ ...v, daily_target: { ...(v.daily_target || {}), enabled: c } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtAmount')}</div>
          <InputNumber style={{ width: '100%' }} min={10} max={1000000} step={50}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).target_usd ?? 500}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), target_usd: x } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtSustain')}</div>
          <InputNumber style={{ width: '100%' }} min={0} max={86400} step={60}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).sustain_sec ?? 30}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), sustain_sec: x } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtAutoClose')}</div>
          <InputNumber style={{ width: '100%' }} min={0} max={86400} step={300}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).auto_close_sec ?? 300}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), auto_close_sec: x } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtPause')}</div>
          <Switch checked={!!((v.daily_target || {}).pause_new ?? true)}
                  disabled={!(v.daily_target || {}).enabled}
                  onChange={(c) => setV({ ...v, daily_target: { ...(v.daily_target || {}), pause_new: c } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtNotify')}</div>
          <Switch checked={!!((v.daily_target || {}).notify ?? true)}
                  disabled={!(v.daily_target || {}).enabled}
                  onChange={(c) => setV({ ...v, daily_target: { ...(v.daily_target || {}), notify: c } })} />
        </Col>
      </Row>
      <div style={{ fontSize: 11, color: '#6b7787', marginTop: 6 }}>{t('risk.dtHint')}</div>
      {Number((v.daily_target || {}).auto_close_sec) > 0 && Number((v.daily_target || {}).auto_close_sec) <= 5 ? (
        <Alert type="error" showIcon style={{ marginTop: 8 }}
               message={t('risk.dtDanger')} />
      ) : null}
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Button size="small" danger
                onClick={async () => {
                  try {
                    const r = await api.post('/api/daily-target/reset', { skip_today: true })
                    message.success(t('risk.dtResetOk'))
                    if (r && r.skipped_today) message.info(t('risk.dtSkipped'))
                    dtRefresh?.(); if (onSaved) onSaved()   // 立刻刷新，否则页面还显示旧状态
                  } catch (e) { message.error(e.message) }
                }}>{t('risk.dtReset')}</Button>
        <Button size="small"
                onClick={async () => {
                  try {
                    await api.post('/api/daily-target/reset', { skip_today: false })
                    message.success(t('risk.dtRearmOk'))
                    dtRefresh?.(); if (onSaved) onSaved()
                  } catch (e) { message.error(e.message) }
                }}>{t('risk.dtRearm')}</Button>
        {dt && dt.skipped_today ? (
          <span style={{ fontSize: 11.5, color: '#ffb020' }}>● {t('risk.dtSkippedNow')}</span>
        ) : null}
      </div>
      <Button type="primary" size="small" style={{ marginTop: 14 }} onClick={save}>
        {t('risk.saveGlobal')}
      </Button>
    </Card>
  )
}

/** 同一根K线同向上限（策略级参数 max_same_bar_side）
 *
 *  为什么不并进上面的「全局风控」表单：这个键住在 strategies.yaml 的 S15 params 里，
 *  用 /api/risk/global 保存会被白名单拒掉（那个接口只认 global 段的键）。
 *  所以它只是**位置**跟「并发」「同向」挨着，保存走策略配置接口。
 *  ★ 0 = 不限（关掉这道限制，engine._same_bar_allow: cap<=0 → 恒 True）
 *    1 = 同一根 15m K 线同向最多 1 笔
 *  ★ 编辑中不能被 15 秒轮询冲掉（2026-11-05 出过陈旧值覆盖回写的事故）
 */
function SameBarCap() {
  const { t } = useI18n()
  const [data, refresh] = usePoll(api.strategies, 15000)
  const rows = (data && data.strategies) || []
  const row = rows.find((r) => (r.params || {}).max_same_bar_side !== undefined)
  const live = row ? row.params.max_same_bar_side : undefined
  const [val, setVal] = useState(null)
  const [orig, setOrig] = useState(null)
  const dirty = React.useRef(false)
  React.useEffect(() => {
    if (dirty.current || live === undefined) return
    setVal(live); setOrig(live)
  }, [live])
  const changed = val !== null && orig !== null && Number(val) !== Number(orig)
  const save = async () => {
    const n = Number(val)
    if (!Number.isFinite(n) || n < 0 || n > 20 || n !== Math.floor(n)) {
      message.error(`${t('risk.sameBar')} ${t('risk.sameBarRange')}`); return
    }
    if (!changed) { message.info(t('risk.noChange')); return }
    try {
      await api.strategyConfig(row.id, { params: { max_same_bar_side: n } })
      dirty.current = false
      message.success(n === 0
        ? t('risk.sameBarSavedOff')
        : `${t('risk.sameBarSaved')} ${n}`)
      refresh?.()
    } catch (e) { message.error(`${t('config.saveFailed')}: ${e.message}`) }
  }
  return (
    <Card size="small" title={t('risk.sameBarTitle')} style={{ marginTop: 16 }}>
      <Row gutter={[16, 8]}>
        <Col xs={24} md={9}>
          <div style={{ fontSize: 12, color: 'var(--mute, #7d8590)', marginBottom: 4 }}>
            {t('risk.sameBar')}
          </div>
          <InputNumber size="small" style={{ width: '100%' }} min={0} max={20} step={1}
                       value={val}
                       onChange={(x) => { dirty.current = true; setVal(x) }} />
          <div style={{ fontSize: 11, color: '#4d5661', marginTop: 2 }}>
            {t('risk.sameBarTip')}（0 ~ 20）
          </div>
        </Col>
        <Col xs={24} md={15}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', lineHeight: 1.9 }}>
            {t('risk.sameBarLayers')}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <Button size="small" type={changed ? 'primary' : 'default'} onClick={save}>
              {t('risk.sameBarSave')}
            </Button>
            <Button size="small" disabled={!changed}
                    onClick={() => { setVal(orig); dirty.current = false }}>
              {t('risk.sameBarUndo')}
            </Button>
            <span style={{ fontSize: 11, color: '#6b7787' }}>
              {live === undefined ? '--' : `${t('risk.sameBarLive')} ${live}`}
              {live !== undefined && Number(live) === 0 ? ` · ${t('risk.sameBarOffState')}` : ''}
            </span>
          </div>
        </Col>
      </Row>
    </Card>
  )
}

function StrategyRiskTable({ data, onSaved }) {
  const { t } = useI18n()
  const [edits, setEdits] = useState({})
  const rows = data?.strategies || []
  const setField = (id, key, val) =>
    setEdits((s) => ({ ...s, [id]: { ...(s[id] || {}), [key]: val } }))
  const rowRisk = (r) => ({ ...(r.risk || {}), ...(edits[r.id] || {}) })

  const save = async (r) => {
    try {
      await api.strategyConfig(r.id, { risk: rowRisk(r) })
      message.success(`${r.id} ${t('risk.riskSaved')}`)
      setEdits((s) => { const n = { ...s }; delete n[r.id]; return n })
      onSaved?.()
    } catch (e) { message.error(`${t('config.saveFailed')}: ${e.message}`) }
  }

  const num = (r, key, props = {}) => (
    <InputNumber size="small" style={{ width: 96 }} value={rowRisk(r)[key]}
                 onChange={(x) => setField(r.id, key, x)} {...props} />
  )

  const columns = [
    { title: t('dash.col.strategy'), key: 'name', width: 190, fixed: 'left',
      render: (_, r) => (
        <span><b>{r.id.split('_')[0]}{r.id.includes('__') ? `#${r.id.split('__')[1]}` : ''}</b>{' '}
          {r.enabled ? <Tag color="green" style={{ fontSize: 10 }}>{t('common.enabled')}</Tag>
                     : <Tag style={{ fontSize: 10 }}>{t('common.disabled')}</Tag>}</span>
      ) },
    { title: t('risk.col.capital'), key: 'capital', width: 120,
      render: (_, r) => num(r, 'capital_usd', { min: 0, step: 100, placeholder: t('risk.ph.allAccount') }) },
    { title: t('risk.col.lev'), key: 'lev', width: 90,
      render: (_, r) => num(r, 'leverage', { min: 1, max: 125 }) },
    { title: t('risk.col.maxpos'), key: 'maxpos', width: 130,
      render: (_, r) => num(r, 'max_position_usd', { min: 0, step: 100, placeholder: t('risk.ph.capLev') }) },
    { title: t('risk.col.rpt'), key: 'rpt', width: 110,
      render: (_, r) => num(r, 'risk_per_trade_pct', { min: 0, step: 0.1, placeholder: t('risk.ph.global') }) },
    { title: t('risk.col.used'), dataIndex: 'used_notional', width: 100,
      render: (v) => fmtUsd(v, 0) },
    { title: '', key: 'save', width: 70, fixed: 'right',
      render: (_, r) => (
        <Button size="small" type={edits[r.id] ? 'primary' : 'default'}
                disabled={!edits[r.id]} onClick={() => save(r)}>{t('common.save')}</Button>
      ) },
  ]

  return (
    <Card size="small" title={t('risk.perStrategy')} style={{ marginTop: 16 }}>
      <Alert type="info" showIcon style={{ marginBottom: 12, fontSize: 12 }}
             message={t('risk.hint')} />
      <Table rowKey="id" dataSource={rows} columns={columns} size="small"
             pagination={false} scroll={{ x: 900 }} />
    </Card>
  )
}

export default function RiskPage() {
  const { t } = useI18n()
  const [risk, refresh] = usePoll(api.risk, 10000)
  return (
    <>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Card size="small">
            <Statistic title={t('dash.equity')} value={risk?.equity ?? '-'}
                       formatter={(v) => (v === '-' ? '-' : fmtUsd(Number(v)))}
                       valueStyle={{ color: 'var(--green, #3fa86a)' }} />
          </Card>
        </Col>
        <Col xs={24} md={16}>
          <GlobalRisk data={risk} onSaved={refresh} />
          <SameBarCap />
        </Col>
      </Row>
      <StrategyRiskTable data={risk} onSaved={refresh} />
    </>
  )
}
