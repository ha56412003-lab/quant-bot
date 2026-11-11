import React from 'react'
import { Card, Col, Row, Statistic, Button, Modal, Input, message } from 'antd'
import { api, READ_ONLY } from '../api'
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
  // 只读观战模式（?ro=1）不显示模式切换 —— 观战的看客不能把盘切去实盘
  if (READ_ONLY) return null
  const [confirmText, setConfirmText] = React.useState('')
  const [pre, setPre] = React.useState(null)     // 预检结果 {ok, items}
  const [open, setOpen] = React.useState(false)
  const [busy, setBusy] = React.useState(false)

  if (!status) return null
  if (status.mode === 'live') {
    return (
      <Button danger size="small" onClick={async () => {
        await api.setMode('paper')
        message.success(t('mode.toPaperOk'))
      }}>{t('mode.toPaper')}</Button>
    )
  }

  // 2026-11-07 浩哥定：点「切换实盘」先自查，不对就弹窗拦住，别让人糊里糊涂进实盘。
  const start = async () => {
    setBusy(true)
    try {
      setPre(await api.livePrecheck())
      setOpen(true)
    } catch (e) {
      message.error('预检请求失败，先别切实盘：' + (e?.message || '网络错误'))
    } finally {
      setBusy(false)
    }
  }

  const doSwitch = async () => {
    try {
      await api.setMode('live', confirmText)
      message.warning(t('mode.liveOn'))
      setOpen(false)
      setConfirmText('')
    } catch (e) {
      // 后端硬闸门拦下（状态刚好变了 / 绕过界面直接调接口）：把问题摊出来
      const d = e?.response?.data?.detail
      if (d && Array.isArray(d.problems)) {
        setPre({ ok: false, items: d.problems.map((p) => ({ ...p, key: p.label, ok: false, required: true })) })
      } else {
        message.error(t('mode.confirmBad'))
      }
    }
  }

  const items = pre?.items || []
  const bad = items.filter((i) => i.required && !i.ok)

  return (
    <>
      <Button size="small" danger ghost loading={busy} onClick={start}>
        {t('mode.toLive')}
      </Button>

      <Modal open={open} width={362} destroyOnClose footer={null} mask={false}
             wrapClassName="lc-wrap"
             onCancel={() => { setOpen(false); setConfirmText('') }}
             title={bad.length ? '还不能切实盘' : t('mode.liveTitle')}>
        {/* 逐条体检结果：✓ 过 / ✕ 拦 / · 仅供参考 */}
        <div className="lc">
          {items.map((i) => (
            <div key={i.key || i.label}
                 className={'lc-i ' + (i.ok ? 'ok' : (i.required ? 'bad' : 'info'))}>
              <span className="lc-m">{i.ok ? '✓' : (i.required ? '✕' : '·')}</span>
              <div className="lc-b">
                <div className="lc-l">{i.label}<em>{i.detail}</em></div>
                {!i.ok && i.fix ? <div className="lc-f">{i.fix}</div> : null}
              </div>
            </div>
          ))}
        </div>

        {bad.length ? (
          <div className="lc-act">
            <Button size="small" onClick={() => setOpen(false)}>知道了</Button>
          </div>
        ) : (
          <>
            <div className="lc-ask">{t('mode.liveDesc')}</div>
            <Input size="small" value={confirmText} style={{ marginTop: 6 }}
                   onChange={(e) => setConfirmText(e.target.value)} placeholder="LIVE" />
            <div className="lc-act">
              <Button size="small" onClick={() => { setOpen(false); setConfirmText('') }}>
                {t('common.cancel')}
              </Button>
              <Button size="small" danger disabled={confirmText !== 'LIVE'} onClick={doSwitch}>
                {t('mode.confirmOk')}
              </Button>
            </div>
          </>
        )}
      </Modal>
    </>
  )
}
