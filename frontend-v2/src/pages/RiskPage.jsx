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
  const [v, setV] = useState({})
  const [orig, setOrig] = useState({})
  React.useEffect(() => {
    setV(g)
    // 记录加载时的原始值：保存时只提交**改过的**字段。
    // 否则表单里任何一个陈旧值都会覆盖掉别人刚改的参数
    // （2026-11-05 事故：max_same_side 被页面里的旧值 6 覆盖回写）
    setOrig(JSON.parse(JSON.stringify(g)))
  }, [data]) // eslint-disable-line
  const fields = [
    ['risk_per_trade_pct', t('risk.f.rpt'), t('risk.f.rptTip')],
    ['max_gross_leverage', t('risk.f.lev'), t('risk.f.levTip')],
    ['max_position_pct_per_coin', t('risk.f.coinCap'), t('risk.f.coinCapTip')],
    ['max_concurrent_coins', t('risk.f.conc'), t('risk.f.concTip')],
    ['max_same_side', t('risk.f.sameSide'), t('risk.f.sameSideTip')],
    ['daily_loss_halt_pct', t('risk.f.halt'), t('risk.f.haltTip')],
    ['event_quiet_minutes', t('risk.f.quiet'), t('risk.f.quietTip')],
  ]
  const save = async () => {
    const diff = {}
    for (const k of Object.keys(v || {})) {
      if (JSON.stringify(v[k]) !== JSON.stringify(orig[k])) diff[k] = v[k]
    }
    if (!Object.keys(diff).length) { message.info(t('risk.noChange')); return }
    try { await api.setGlobalRisk(diff); message.success(t('risk.globalSaved')); onSaved?.() }
    catch (e) { message.error(`${t('config.saveFailed')}: ${e.message}`) }
  }
  return (
    <Card size="small" title={t('risk.global')}>
      <Row gutter={[16, 12]}>
        {fields.map(([k, label, tip]) => (
          <Col xs={12} md={8} key={k}>
            <div style={{ fontSize: 12, color: '#7d8590', marginBottom: 4 }}>{label}</div>
            <InputNumber size="small" style={{ width: '100%' }} value={v[k]}
                         onChange={(x) => setV((s) => ({ ...s, [k]: x }))} />
            <div style={{ fontSize: 11, color: '#4d5661', marginTop: 2 }}>{tip}</div>
          </Col>
        ))}
      </Row>

      <Divider style={{ margin: '14px 0 10px', borderColor: 'rgba(193,32,126,0.2)' }} />
      <div style={{ fontSize: 12.5, color: '#9beeff', fontWeight: 700, marginBottom: 10 }}>
        {t('risk.dailyTarget')}
      </div>
      <Row gutter={[16, 12]}>
        <Col xs={24} md={5}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtEnable')}</div>
          <Switch checked={!!(v.daily_target || {}).enabled}
                  onChange={(c) => setV({ ...v, daily_target: { ...(v.daily_target || {}), enabled: c } })} />
        </Col>
        <Col xs={24} md={5}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtAmount')}</div>
          <InputNumber style={{ width: '100%' }} min={10} step={50}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).target_usd ?? 200}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), target_usd: x } })} />
        </Col>
        <Col xs={24} md={5}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtSustain')}</div>
          <InputNumber style={{ width: '100%' }} min={0} step={60}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).sustain_sec ?? 300}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), sustain_sec: x } })} />
        </Col>
        <Col xs={24} md={5}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtAutoClose')}</div>
          <InputNumber style={{ width: '100%' }} min={0} step={300}
                       disabled={!(v.daily_target || {}).enabled}
                       value={(v.daily_target || {}).auto_close_sec ?? 1800}
                       onChange={(x) => setV({ ...v, daily_target: { ...(v.daily_target || {}), auto_close_sec: x } })} />
        </Col>
        <Col xs={24} md={4}>
          <div style={{ fontSize: 11.5, color: '#8a96a8', marginBottom: 4 }}>{t('risk.dtNotify')}</div>
          <Switch checked={!!((v.daily_target || {}).notify ?? true)}
                  disabled={!(v.daily_target || {}).enabled}
                  onChange={(c) => setV({ ...v, daily_target: { ...(v.daily_target || {}), notify: c } })} />
        </Col>
      </Row>
      <div style={{ fontSize: 11, color: '#6b7787', marginTop: 6 }}>{t('risk.dtHint')}</div>
      <Button size="small" style={{ marginTop: 8 }}
              onClick={async () => {
                try { await api.post('/api/daily-target/reset', {}); message.success(t('risk.dtResetOk')) }
                catch (e) { message.error(e.message) }
              }}>{t('risk.dtReset')}</Button>
      <Button type="primary" size="small" style={{ marginTop: 14 }} onClick={save}>
        {t('risk.saveGlobal')}
      </Button>
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
                       valueStyle={{ color: '#3fa86a' }} />
          </Card>
        </Col>
        <Col xs={24} md={16}>
          <GlobalRisk data={risk} onSaved={refresh} />
        </Col>
      </Row>
      <StrategyRiskTable data={risk} onSaved={refresh} />
    </>
  )
}
