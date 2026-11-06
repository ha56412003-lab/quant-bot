import { Card, Table, Tabs, Tag, Typography } from 'antd'
import { fmtPnl } from '../api'
import { useI18n } from '../i18n'

export default function ActivityTabs({ signals, trades, logs, gates }) {
  const { t, lang } = useI18n()
  const ts = (v) => new Date(v * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  const signalCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('dash.col.strategy'), dataIndex: 'strategy', width: 60, render: (v) => v.split('_')[0] },
    { title: t('dash.col.symbol'), dataIndex: 'symbol', width: 80 },
    { title: t('dash.col.side'), dataIndex: 'side', width: 50,
      render: (v) => <Tag color={v === 'long' ? 'red' : 'green'}>{v === 'long' ? t('dash.long') : t('dash.short')}</Tag> },
    { title: t('act.result'), key: 'acted', width: 110,
      render: (_, r) => {
        if (!r.acted) return <Tag color="default">{r.skip_reason || t('act.skipped')}</Tag>
        // acted=1 只说明「当时开了仓」，仓位现在还在不在要看 pos_status
        if (r.pos_status === 'open') return <Tag color="green">{t('act.holding')}</Tag>
        if (r.pos_status === 'closed') {
          const p = r.pos_pnl
          return <Tag color="default">{t('act.closed')}{p == null ? '' : ' '}
            {p != null && <span style={{ color: p >= 0 ? '#d4595e' : '#3fa86a' }}>{fmtPnl(p)}</span>}
          </Tag>
        }
        return <Tag color="blue">{t('act.executed')}</Tag>
      } },
    { title: t('act.reason'), dataIndex: 'reason', ellipsis: true },
  ]
  const tradeCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('dash.col.strategy'), dataIndex: 'strategy', width: 60, render: (v) => v.split('_')[0] },
    { title: t('dash.col.symbol'), dataIndex: 'symbol', width: 80 },
    { title: t('act.kind'), dataIndex: 'kind', width: 55,
      render: (v) => <Tag color={v === 'open' ? 'blue' : 'purple'}>{v === 'open' ? t('dash.openMark') : t('dash.closeMark')}</Tag> },
    { title: t('act.price'), dataIndex: 'price', width: 90, render: (v) => v?.toPrecision(6) },
    { title: t('dash.col.qty'), dataIndex: 'qty', width: 80, render: (v) => v?.toPrecision(4) },
    { title: t('journal.col.pnl'), dataIndex: 'pnl', width: 80,
      render: (v) => v == null ? '-' :
        <span style={{ color: v >= 0 ? '#d4595e' : '#3fa86a' }}>{fmtPnl(v)}</span> },
    { title: t('journal.col.mode'), dataIndex: 'mode', width: 60,
      render: (v) => <Tag color={v === 'live' ? 'red' : 'blue'} style={{ fontSize: 10 }}>{v}</Tag> },
  ]
  const logCols = [
    { title: t('act.time'), dataIndex: 'ts', width: 96, render: ts },
    { title: t('act.level'), dataIndex: 'level', width: 55,
      render: (v) => <Tag color={{ error: 'red', warn: 'orange' }[v] || 'default'}>{v}</Tag> },
    { title: t('act.msg'), dataIndex: 'msg',
      render: (v) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> },
  ]
  const gateCols = [
    { title: t('gate.col.symbol'), dataIndex: 'symbol', width: 72,
      render: (v, r) => (
        <span>
          {v.replace('USDT', '')}
          {!r.in_monitor && <Tag color="default" style={{ marginLeft: 2, fontSize: 9, padding: '0 3px' }}>{t('gate.outPool')}</Tag>}
        </span>
      ) },
    { title: t('gate.col.dir'), dataIndex: 'dir', width: 40,
      render: (v) => <Tag color={v === '多' ? 'red' : 'green'} style={{ margin: 0, padding: '0 4px' }}>{v}</Tag> },
    { title: t('gate.col.ce'), dataIndex: 'ce', width: 52, align: 'center',
      render: (v) => v ? <span style={{ color: '#52c41a' }}>✓</span> : <span style={{ color: '#8c8c8c' }}>✗</span> },
    { title: t('gate.col.ha'), dataIndex: 'ha', width: 52, align: 'center',
      render: (v) => v ? <span style={{ color: '#52c41a' }}>✓</span> : <span style={{ color: '#8c8c8c' }}>✗</span> },
    { title: t('gate.col.bbw'), dataIndex: 'bbw', width: 88, align: 'right',
      render: (v, r) => {
        if (v == null) return <span style={{ color: '#8c8c8c' }}>-</span>
        const pass = v >= (r.bbw_thr || 70)
        return <span style={{ color: pass ? '#52c41a' : '#8c8c8c', fontSize: 12 }}>
          {v.toFixed(0)}%<span style={{ fontSize: 9 }}>/{r.bbw_thr || 70}</span>
        </span>
      } },
    { title: t('gate.col.why'), dataIndex: 'why', width: 115,
      render: (v, r) => r.ok
        ? <Tag color="green" style={{ margin: 0 }}>{t('gate.opened')}</Tag>
        : <span style={{ fontSize: 12, color: '#8c8c8c', whiteSpace: 'normal' }}>{v}</span> },
  ]
  const g = gates || {}
  const gateRows = g.rows || []
  const gateBar = g.ts ? new Date(g.ts * 1000).toLocaleTimeString(lang === 'en' ? 'en-US' : 'zh-CN',
    { hour: '2-digit', minute: '2-digit' }) : '-'
  const items = [
    { key: 'gates',
      label: `${t('act.gates')} ${gateRows.length ? `(${g.passed || 0}/${gateRows.length})` : ''}`,
      children: (
        <div>
          <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 6 }}>
            {t('gate.bar')} {gateBar} · {t('gate.summary')({
              ce: g.ce_n || 0, ha: g.ha_n || 0, bbw: g.bbw_n || 0, n: gateRows.length,
            })}
          </div>
          <Table rowKey="symbol" dataSource={gateRows} columns={gateCols} size="small"
                 pagination={false} scroll={{ y: 330 }} />
        </div>
      ) },
    { key: 'signals', label: `${t('act.signals')} (${signals.length})`,
      children: <Table rowKey="id" dataSource={signals} columns={signalCols} size="small"
                       pagination={false} scroll={{ y: 330 }} /> },
    { key: 'trades', label: `${t('act.trades')} (${trades.length})`,
      children: <Table rowKey="id" dataSource={trades} columns={tradeCols} size="small"
                       pagination={false} scroll={{ y: 330 }} /> },
    { key: 'logs', label: t('act.logs'),
      children: <Table rowKey="id" dataSource={logs} columns={logCols} size="small"
                       pagination={false} scroll={{ y: 330 }} /> },
  ]
  return (
    <Card size="small">
      <Tabs items={items} size="small" />
    </Card>
  )
}
