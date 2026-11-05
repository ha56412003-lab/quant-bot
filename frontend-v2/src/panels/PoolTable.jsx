import { useEffect, useRef, useState } from 'react'
import { Card, Table, Tag, Tooltip, Typography } from 'antd'
import { useI18n } from '../i18n'

const { Text } = Typography

function fmtVol(v) {
  if (v == null) return '-'
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(0)} 万`
  return v.toFixed(0)
}

function ago(ts) {
  if (!ts) return '-'
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 60) return `${s}秒前`
  if (s < 3600) return `${Math.floor(s / 60)}分前`
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`
  return `${Math.floor(s / 86400)}天前`
}

export default function PoolTable({ pool }) {
  const { t } = useI18n()
  // 动态表格高度：撑满卡片，不留底部空白
  const boxRef = useRef(null)
  const [scrollY, setScrollY] = useState(320)
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const host = el.closest('.pnl') || el.parentElement
    const calc = () => {
      const pnlH = host.getBoundingClientRect().height
      const headEl = el.querySelector('.ant-table-thead')
      const headH = headEl ? headEl.getBoundingClientRect().height : 32
      const avail = pnlH - headH - 40 - 16    // 40 = Card 上下 padding，16 = 横滚条
      if (avail < 120) return
      const N = Math.max(6, Math.round(avail / 46))   // 目标每行 46px
      const rowH = Math.floor(avail / N)              // 均分 → 底部零留白
      el.style.setProperty('--row-h', rowH + 'px')
      setScrollY(Math.floor(rowH * N))
    }
    calc()
    requestAnimationFrame(calc)
    const ro = new ResizeObserver(calc)
    ro.observe(host)
    return () => ro.disconnect()
  }, [])
  const rows = (pool?.rows || []).slice().sort((a, b) => {
    const rank = (r) => (r.held ? 0 : r.in_monitor ? 1 : 2)
    const d = rank(a) - rank(b)
    return d !== 0 ? d : (b.qv || 0) - (a.qv || 0)
  })
  const blocked = rows.filter((r) => r.ok === false).length
  const held = rows.filter((r) => r.held).length

  const columns = [
    {
      title: t('pool.col.symbol'), dataIndex: 'symbol', width: 90, fixed: 'left',
      render: (v, r) => (
        <span>
          <Text strong>{v.replace('USDT', '')}</Text>
          {r.held && <Tag color="green" style={{ marginLeft: 6, fontSize: 10 }}>{t('pool.held')}</Tag>}
        </span>
      ),
    },
    {
      title: t('pool.col.price'), dataIndex: 'price', align: 'right', width: 100,
      render: (v) => (v ? (v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toPrecision(4)) : '-'),
    },
    {
      title: t('pool.col.vol'), dataIndex: 'qv', align: 'right', width: 95,
      sorter: (a, b) => (a.qv || 0) - (b.qv || 0),
      render: (v) => (
        <span style={{ color: v != null && v < 1e7 ? '#d4595e' : undefined }}>{fmtVol(v)}</span>
      ),
    },
    {
      title: 'ATR%', dataIndex: 'atr_pct', align: 'right', width: 80,
      render: (v) => (v != null ? `${v.toFixed(2)}%` : '-'),
    },
    {
      title: t('pool.col.spike'), dataIndex: 'spike', align: 'right', width: 85,
      sorter: (a, b) => (a.spike || 0) - (b.spike || 0),
      render: (v) => {
        const cap = pool?.max_spike ?? 4.0
        const over = v != null && v > cap
        return (
          <span style={{ color: over ? '#d4595e' : undefined, fontWeight: over ? 700 : 400 }}>
            {v != null ? v.toFixed(2) : '-'}
          </span>
        )
      },
    },
    {
      title: '池', dataIndex: 'in_monitor', align: 'center', width: 62,
      filters: [
        { text: '监控池内', value: true },
        { text: '监控池外', value: false },
      ],
      onFilter: (val, r) => r.in_monitor === val,
      render: (v, r) => {
        if (r.held) {
          return r.in_candidate === false
            ? <Tag color="gold" style={{ fontSize: 10 }} title="已掉出候选池，但持仓不受影响">持仓·出池</Tag>
            : <Tag color="gold" style={{ fontSize: 10 }}>持仓</Tag>
        }
        return v
          ? <Tag color="cyan" style={{ fontSize: 10 }}>监控</Tag>
          : <Tag style={{ fontSize: 10, opacity: .55 }}>候选</Tag>
      },
    },
    {
      title: t('pool.col.filter'), dataIndex: 'ok', align: 'center', width: 92,
      filters: [
        { text: t('pool.pass'), value: true },
        { text: t('pool.blocked'), value: false },
      ],
      onFilter: (val, r) => r.ok === val,
      render: (v, r) => {
        if (v == null) return <Tag>{t('pool.unknown')}</Tag>
        return v
          ? <Tag color="green">{t('pool.pass')}</Tag>
          : <Tooltip title={r.why}><Tag color="red">{t('pool.blocked')} {r.why}</Tag></Tooltip>
      },
    },
    {
      title: t('pool.col.signal'), key: 'sig', width: 150,
      render: (_, r) => {
        if (!r.last_sig_side) return <Text type="secondary">-</Text>
        return (
          <span style={{ fontSize: 12 }}>
            <Tag color={r.last_sig_side === 'long' ? 'red' : 'green'}>
              {r.last_sig_side === 'long' ? t('pool.long') : t('pool.short')}
            </Tag>
            <span style={{ color: r.last_sig_acted ? '#52c41a' : '#8c8c8c' }}>
              {r.last_sig_acted ? t('pool.acted') : (r.last_sig_skip || t('pool.skipped'))}
            </span>
            <div style={{ color: '#8c8c8c', fontSize: 11 }}>{ago(r.last_sig_ts)}</div>
          </span>
        )
      },
    },
  ]

  return (
    <Card ref={boxRef}
      size="small"
      title={`${t('pool.title')}`}
      extra={
        <span style={{ fontSize: 12, color: '#8c8c8c' }}>
          <span style={{ color: '#7d8590' }}>候选</span> <b style={{ color: '#e6edf3' }}>{pool?.candidate_n ?? rows.length}</b>
          <span style={{ margin: '0 4px', color: '#30363d' }}>→</span>
          <span style={{ color: '#7d8590' }}>监控</span> <b style={{ color: '#45e6f5' }}>{pool?.monitor_n ?? '-'}</b>
          <span style={{ margin: '0 4px', color: '#30363d' }}>→</span>
          <span style={{ color: '#7d8590' }}>持仓</span> <b style={{ color: '#ff4d5e' }}>{pool?.position_n ?? held}</b>
          {pool?.ts ? <span style={{ marginLeft: 10, color: '#7d8590' }}>{ago(pool.ts)}{t('pool.updated')}</span> : null}
        </span>
      }
    >
      <Table
        rowKey="symbol"
        dataSource={rows}
        columns={columns}
        size="small"
        pagination={false}
        scroll={{ x: 'max-content', y: scrollY }}
        rowClassName={(r) => (r.ok === false ? 'pool-row-blocked' : '')}
        locale={{ emptyText: t('pool.empty') }}
      />
    </Card>
  )
}
