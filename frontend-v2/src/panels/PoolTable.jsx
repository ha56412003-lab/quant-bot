import { useEffect, useRef, useState } from 'react'
import { Card, Table, Tag, Tooltip, Typography } from 'antd'
import { useI18n } from '../i18n'
import { fmtPnl } from '../api'

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
      // 16px 横滚条预留已取消：表格不再横向滚动（2026-11-06 去掉 scroll.x）
      const avail = pnlH - headH - 40    // 40 = Card 上下 padding
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
      // 列宽按 1920 窗口下容器 602px 反推：8 列合计 572px，留 30px 余量。
      // 不再 fixed:'left' —— 横向滚动没了，fixed 列没有意义。
      title: t('pool.col.symbol'), dataIndex: 'symbol', width: 80,
      render: (v, r) => (
        <span style={{ whiteSpace: 'nowrap' }}>
          <Text strong>{v.replace('USDT', '')}</Text>
          {r.held && (
            <Tag color="green" style={{ marginLeft: 3, fontSize: 9, padding: '0 3px', lineHeight: '14px' }}>持</Tag>
          )}
        </span>
      ),
    },
    {
      title: t('pool.col.price'), dataIndex: 'price', align: 'right', width: 78,
      render: (v) => (v ? (v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toPrecision(4)) : '-'),
    },
    {
      title: t('pool.col.vol'), dataIndex: 'qv', align: 'right', width: 72,
      sorter: (a, b) => (a.qv || 0) - (b.qv || 0),
      render: (v) => (
        <span style={{ color: v != null && v < 1e7 ? 'var(--red, #d4595e)' : undefined }}>{fmtVol(v)}</span>
      ),
    },
    {
      title: 'ATR%', dataIndex: 'atr_pct', align: 'right', width: 58,
      render: (v) => (v != null ? `${v.toFixed(2)}%` : '-'),
    },
    {
      title: t('pool.col.spike'), dataIndex: 'spike', align: 'right', width: 56,
      sorter: (a, b) => (a.spike || 0) - (b.spike || 0),
      render: (v) => {
        const cap = pool?.max_spike ?? 4.0
        const over = v != null && v > cap
        return (
          <span style={{ color: over ? 'var(--red, #d4595e)' : undefined, fontWeight: over ? 700 : 400 }}>
            {v != null ? v.toFixed(2) : '-'}
          </span>
        )
      },
    },
    {
      title: '池', dataIndex: 'in_monitor', align: 'center', width: 50,
      filters: [
        { text: '监控池内', value: true },
        { text: '监控池外', value: false },
      ],
      onFilter: (val, r) => r.in_monitor === val,
      render: (v, r) => {
        if (r.held) {
          return r.in_candidate === false
            ? <Tooltip title="已掉出候选池，但持仓不受影响"><Tag color="gold" style={{ fontSize: 9, padding: '0 3px' }}>持·出</Tag></Tooltip>
            : <Tag color="gold" style={{ fontSize: 9, padding: '0 3px' }}>持</Tag>
        }
        return v
          ? <Tag color="cyan" style={{ fontSize: 9, padding: '0 3px' }}>监控</Tag>
          : <Tag style={{ fontSize: 9, padding: '0 3px', opacity: .55 }}>候选</Tag>
      },
    },
    {
      // 原因文字以前直接塞进 Tag 里，把 92px 的列撑到 122px —— 这是横向滚动的主犯。
      // 改成 Tag 只写结论，原因进 tooltip。
      title: t('pool.col.filter'), dataIndex: 'ok', align: 'center', width: 62,
      filters: [
        { text: t('pool.pass'), value: true },
        { text: t('pool.blocked'), value: false },
      ],
      onFilter: (val, r) => r.ok === val,
      render: (v, r) => {
        if (v == null) return <Tag>{t('pool.unknown')}</Tag>
        return v
          ? <Tag color="green">{t('pool.pass')}</Tag>
          : <Tooltip title={r.why}><Tag color="red">{t('pool.blocked')}</Tag></Tooltip>
      },
    },
    {
      // 原来 Tag + 状态 + 时间挤在 150px 里换 3 行。改成两行：
      // 第一行 Tag + 状态（超长截断，全文进 tooltip），第二行时间。
      title: t('pool.col.signal'), key: 'sig', width: 116,
      render: (_, r) => {
        if (!r.last_sig_side) return <Text type="secondary">-</Text>
        const sideTag = (
          <Tag color={r.last_sig_side === 'long' ? 'red' : 'green'}
               style={{ fontSize: 9, padding: '0 3px', lineHeight: '14px', marginInlineEnd: 4 }}>
            {r.last_sig_side === 'long' ? t('pool.long') : t('pool.short')}
          </Tag>
        )
        let body
        if (!r.last_sig_acted) {
          // 方向已经由前面的 Tag 表达，这里把「空头/多头/做空/做多」前缀剥掉，
          // 否则读出来是「空 · 空头方向已达上限」——白占 2 个字的宽度还别扭
          let full = (r.last_sig_skip || t('pool.skipped')).replace(/^(空头|多头|做空|做多)/, '')
          const short = full.length > 6 ? `${full.slice(0, 6)}…` : full
          body = <Tooltip title={r.last_sig_skip || t('pool.skipped')}><span style={{ color: 'var(--mute, #8c8c8c)' }}>{short}</span></Tooltip>
        } else if (r.last_sig_pos === 'closed') {
          const p = r.last_sig_pnl
          body = (
            <span style={{ color: 'var(--mute, #8c8c8c)' }}>
              {t('act.closed')}{' '}
              {p != null && <span style={{ color: p >= 0 ? 'var(--red, #c4182c)' : 'var(--green, #0f7a3d)' }}>{fmtPnl(p)}</span>}
            </span>
          )
        } else if (r.last_sig_pos === 'open') {
          body = <span style={{ color: 'var(--green, #52c41a)' }}>{t('pool.held')}</span>
        } else {
          body = <span style={{ color: '#1677ff' }}>{t('pool.acted')}</span>
        }
        return (
          <div style={{ fontSize: 11, whiteSpace: 'nowrap' }}>
            <div>{sideTag}{body}</div>
            <div style={{ color: 'var(--mute, #8c8c8c)', fontSize: 10 }}>{ago(r.last_sig_ts)}</div>
          </div>
        )
      },
    },
  ]

  return (
    <Card ref={boxRef}
      size="small"
      title={`${t('pool.title')}`}
      extra={
        <span style={{ fontSize: 12, color: 'var(--mute, #8c8c8c)' }}>
          <span style={{ color: 'var(--mute, #7d8590)' }}>候选</span> <b style={{ color: 'var(--ink, #e6edf3)' }}>{pool?.candidate_n ?? rows.length}</b>
          <span style={{ margin: '0 4px', color: 'var(--dim, #30363d)' }}>→</span>
          <span style={{ color: 'var(--mute, #7d8590)' }}>监控</span> <b style={{ color: 'var(--cyan, #45e6f5)' }}>{pool?.monitor_n ?? '-'}</b>
          <span style={{ margin: '0 4px', color: 'var(--dim, #30363d)' }}>→</span>
          <span style={{ color: 'var(--mute, #7d8590)' }}>持仓</span> <b style={{ color: 'var(--red, #ff4d5e)' }}>{pool?.position_n ?? held}</b>
          {pool?.ts ? <span style={{ marginLeft: 10, color: 'var(--mute, #7d8590)' }}>{ago(pool.ts)}{t('pool.updated')}</span> : null}
        </span>
      }
    >
      <Table
        rowKey="symbol"
        dataSource={rows}
        columns={columns}
        size="small"
        pagination={false}
        // tableLayout="fixed" + 不传 scroll.x：列宽严格按容器分配，
        // 容器再窄也只是内容压缩，不会冒出横向滚动条（浩哥明确要求不要横向滑块）。
        // scroll.y 保留 —— 纵向虚拟滚动是这个表格撑满卡片高度的关键。
        tableLayout="fixed"
        scroll={{ y: scrollY }}
        rowClassName={(r) => (r.ok === false ? 'pool-row-blocked' : '')}
        locale={{ emptyText: t('pool.empty') }}
      />
    </Card>
  )
}
