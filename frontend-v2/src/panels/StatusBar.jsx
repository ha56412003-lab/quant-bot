import React from 'react'
import { Card, Col, Row, Statistic, Button, Popconfirm, Input, message } from 'antd'
import { api, fmtUsd } from '../api'
import { useI18n } from '../i18n'

export function StatusCards({ status, positions }) {
  const { t } = useI18n()
  const upnl = positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)
  // ① 保证金使用率  ③ 可用保证金
  const marginUsed = positions.reduce((s, p) => s + (p.margin || 0), 0)
  const equity = Number(status?.equity) || 0
  const marginRatio = equity > 0 ? (marginUsed / equity) * 100 : 0
  const marginFree = equity - marginUsed
  const ratioColor = marginRatio >= 60 ? 'var(--red, #d4595e)' : marginRatio >= 40 ? '#d48806' : 'var(--green, #3fa86a)'
  const cards = [
    { title: t('dash.equity'), value: status?.equity, formatter: (v) => fmtUsd(v) },
    { title: t('dash.upnl'), value: upnl, color: upnl >= 0 ? 'var(--red, #d4595e)' : 'var(--green, #3fa86a)',
      formatter: (v) => `${v >= 0 ? '+' : ''}${fmtUsd(v)}` },
    { title: t('dash.dayPnl'), value: status?.daily_realized_pnl,
      color: (status?.daily_realized_pnl || 0) >= 0 ? 'var(--red, #d4595e)' : 'var(--green, #3fa86a)',
      formatter: (v) => `${v >= 0 ? '+' : ''}${fmtUsd(v)}` },
    { title: t('dash.posCount'), value: positions.length, formatter: (v) => v },
    { title: t('dash.marginUsage'), value: marginRatio, suffix: '%', color: ratioColor,
      formatter: (v) => Number(v).toFixed(1) },
    { title: t('dash.marginFree'), value: marginFree, color: 'var(--green, #3fa86a)',
      formatter: (v) => fmtUsd(v) },
  ]
  return (
    <Row gutter={[16, 16]}>
      {cards.map((c) => (
        <Col xs={12} lg={4} key={c.title}>
          <Card size="small">
            <Statistic title={c.title} value={c.value ?? '-'} suffix={c.suffix}
                       valueStyle={{ color: c.color, fontSize: 22 }}
                       formatter={(v) => (v === '-' ? '-' : c.formatter(Number(v)))} />
          </Card>
        </Col>
      ))}
    </Row>
  )
}

export function ModeSwitch({ status }) {
  const { t } = useI18n()
  const [confirmText, setConfirmText] = React.useState('')
  if (!status) return null
  if (status.mode === 'live') {
    return (
      <Button danger size="small" onClick={async () => {
        await api.setMode('paper')
        message.success(t('mode.toPaperOk'))
      }}>{t('mode.toPaper')}</Button>
    )
  }
  return (
    <Popconfirm
      title={t('mode.liveTitle')}
      description={
        <div style={{ maxWidth: 280 }}>
          {t('mode.liveDesc')}
          <Input size="small" value={confirmText} style={{ marginTop: 6 }}
                 onChange={(e) => setConfirmText(e.target.value)} placeholder="LIVE" />
        </div>
      }
      okText={t('mode.confirmOk')} cancelText={t('common.cancel')}
      onConfirm={async () => {
        try {
          await api.setMode('live', confirmText)
          message.warning(t('mode.liveOn'))
        } catch {
          message.error(t('mode.confirmBad'))
        }
        setConfirmText('')
      }}
    >
      <Button size="small" danger ghost>{t('mode.toLive')}</Button>
    </Popconfirm>
  )
}
