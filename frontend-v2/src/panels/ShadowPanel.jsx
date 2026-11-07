import { Card, Table, Tag, Row, Col, Statistic, Tooltip, Typography, Empty } from 'antd'
import { useI18n } from '../i18n'

const { Text } = Typography

function RuleBox({ title, tip, pass, block, t }) {
  const has = (pass?.n || 0) + (block?.n || 0) > 0
  return (
    <Card size="small" style={{ height: '100%' }}>
      <Tooltip title={tip}>
        <div style={{ fontSize: 13, marginBottom: 8, color: 'var(--mute, #8c8c8c)' }}>{title}</div>
      </Tooltip>
      {!has ? (
        <Text type="secondary" style={{ fontSize: 12 }}>{t('shadow.collecting')}</Text>
      ) : (
        <Row gutter={8}>
          <Col span={12}>
            <Statistic
              title={<span style={{ fontSize: 11 }}>{t('shadow.pass')} ({pass.n})</span>}
              value={pass.n ? pass.win : 0} suffix="%" precision={1}
              valueStyle={{ fontSize: 18, color: 'var(--green, #52c41a)' }}
            />
            <div style={{ fontSize: 11, color: 'var(--mute, #8c8c8c)' }}>
              {t('shadow.avgR')} {pass.n ? pass.avg_r.toFixed(3) : '-'}
            </div>
          </Col>
          <Col span={12}>
            <Statistic
              title={<span style={{ fontSize: 11 }}>{t('shadow.block')} ({block.n})</span>}
              value={block.n ? block.win : 0} suffix="%" precision={1}
              valueStyle={{ fontSize: 18, color: block.n && block.win < pass.win ? 'var(--red, #d4595e)' : '#d48806' }}
            />
            <div style={{ fontSize: 11, color: 'var(--mute, #8c8c8c)' }}>
              {t('shadow.avgR')} {block.n ? block.avg_r.toFixed(3) : '-'}
            </div>
          </Col>
        </Row>
      )}
      {has && (
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--mute, #8c8c8c)' }}>
          {block.n && pass.n
            ? (block.win < pass.win
                ? t('shadow.ruleWorks')
                : t('shadow.ruleUseless'))
            : ''}
        </div>
      )}
    </Card>
  )
}

export default function ShadowPanel({ shadow }) {
  const { t } = useI18n()
  const s = shadow?.stats || {}
  const rows = shadow?.recent || []
  const settled = (s.all?.n || 0)

  const columns = [
    {
      title: t('shadow.col.time'), dataIndex: 'ts', width: 78,
      render: (v) => (v ? new Date(v * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '-'),
    },
    { title: t('shadow.col.coin'), dataIndex: 'symbol', width: 78,
      render: (v) => v.replace('USDT', '') },
    { title: t('shadow.col.side'), dataIndex: 'side', width: 52,
      render: (v) => <Tag color={v === 'long' ? 'red' : 'green'}>{v === 'long' ? t('pool.long') : t('pool.short')}</Tag> },
    { title: 'funding', dataIndex: 'funding', width: 74, align: 'right',
      render: (v) => (v == null ? '-' : `${v > 0 ? '+' : ''}${v.toFixed(3)}%`) },
    { title: 'taker', dataIndex: 'taker', width: 66, align: 'right',
      render: (v) => (v == null ? '-' : v.toFixed(2)) },
    {
      title: t('shadow.col.rules'), key: 'rules', width: 150,
      render: (_, r) => (
        <span style={{ fontSize: 11 }}>
          {r.r1 ? <Tag color="red">①{t('shadow.block')}</Tag> : <Tag color="green">①{t('shadow.pass')}</Tag>}
          {r.r2 ? <Tag color="red">②{t('shadow.block')}</Tag> : <Tag color="green">②{t('shadow.pass')}</Tag>}
          {r.r3 ? <Tag color="orange">③½</Tag> : null}
        </span>
      ),
    },
    {
      title: t('shadow.col.acted'), dataIndex: 'acted', width: 66,
      render: (v) => (v ? <Tag color="blue">{t('shadow.opened')}</Tag> : <Tag>{t('shadow.skipped')}</Tag>),
    },
    {
      title: t('shadow.col.result'), key: 'res', width: 92,
      render: (_, r) => {
        if (!r.outcome) return <Text type="secondary" style={{ fontSize: 11 }}>{t('shadow.pending')}</Text>
        const pos = (r.outcome_r || 0) > 0
        return (
          <span style={{ fontSize: 11 }}>
            <Tag color={r.outcome === 'tp' ? 'green' : r.outcome === 'stop' ? 'red' : 'default'}>{r.outcome}</Tag>
            <span style={{ color: pos ? 'var(--green, #52c41a)' : 'var(--red, #d4595e)' }}>
              {r.outcome_r > 0 ? '+' : ''}{(r.outcome_r || 0).toFixed(2)}R
            </span>
          </span>
        )
      },
    },
  ]

  return (
    <Card
      size="small"
      title={
        <span>
          {t('shadow.title')}
          <span style={{ fontSize: 11, color: 'var(--mute, #8c8c8c)', marginLeft: 8, fontWeight: 400 }}>
            {t('shadow.subtitle')}
          </span>
        </span>
      }
      extra={<span style={{ fontSize: 12, color: 'var(--mute, #8c8c8c)' }}>{t('shadow.settled')} {settled}</span>}
    >
      <Row gutter={[10, 10]}>
        <Col xs={24} md={8}>
          <RuleBox
            title={t('shadow.r1')}
            tip={t('shadow.r1Tip')}
            pass={s.r1_pass} block={s.r1_block} t={t}
          />
        </Col>
        <Col xs={24} md={8}>
          <RuleBox
            title={t('shadow.r2')}
            tip={t('shadow.r2Tip')}
            pass={s.r2_pass} block={s.r2_block} t={t}
          />
        </Col>
        <Col xs={24} md={8}>
          <Card size="small" style={{ height: '100%' }}>
            <Tooltip title={t('shadow.r3Tip')}>
              <div style={{ fontSize: 13, marginBottom: 8, color: 'var(--mute, #8c8c8c)' }}>{t('shadow.r3')}</div>
            </Tooltip>
            {!s.r3_half?.n ? (
              <Text type="secondary" style={{ fontSize: 12 }}>{t('shadow.collecting')}</Text>
            ) : (
              <Row gutter={8}>
                <Col span={12}>
                  <Statistic title={<span style={{ fontSize: 11 }}>{t('shadow.halfN')}</span>}
                    value={s.r3_half.n} valueStyle={{ fontSize: 18 }} />
                </Col>
                <Col span={12}>
                  <Statistic title={<span style={{ fontSize: 11 }}>{t('shadow.winRate')}</span>}
                    value={s.r3_half.win} suffix="%" precision={1}
                    valueStyle={{ fontSize: 18, color: '#d48806' }} />
                </Col>
              </Row>
            )}
          </Card>
        </Col>
      </Row>

      <div style={{ marginTop: 10 }}>
        {rows.length === 0 ? (
          <Empty description={t('shadow.empty')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Table
            rowKey="id" dataSource={rows} columns={columns}
            size="small" pagination={false}
          />
        )}
      </div>
    </Card>
  )
}
