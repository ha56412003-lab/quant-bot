import React from 'react'
import { Card, Table, Switch, InputNumber, Button, Tag, Typography, Space, message, Tooltip } from 'antd'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'

const { Text } = Typography

/**
 * S15 五道闸手动调参
 * ─────────────────────────────────────────────────────────────
 * 位置：策略库页面 S15 策略表格下方
 * 数据源：全部从 row.params（后端 strategies.yaml 实时值）读，前端不写死任何门槛
 * 保存：走 onParamsSave(row.id, patch) → api.strategyConfig → strategies.yaml
 *       ★ 网页改参数即时生效，不用重启（config.load_strategies() 每轮重读，TICK_SEC=60）
 *
 * ★ 闸门顺序与「光闸面板」(ActivityTabs) 的 5 列严格一致：
 *     时段 → HA → 关键K → 止损距离 → EFF
 *   偏离闸在 VS15-1.1 已从后端物理删除，VS15-1.2 把面板这一行也删了（不再留灰显占位）。
 *   BBW 闸在 2026-11-12 按浩哥指令连代码带配置键一起物理删除（六道闸 → 五道闸）。
 *
 * ★ 文案原则（2026-11-12 浩哥要求「这些都看不懂啥意思」）：
 *   一律大白话 —— 先说「这道闸在问什么问题」，再说数值是什么意思。
 *   不许出现 ATR / 分位 / 实体 / 单边度 这类词就完事，必须紧跟一句人话解释。
 * ─────────────────────────────────────────────────────────────
 */

const GATES = [
  {
    no: '①', id: 'ce', name: '时段确认',
    q: '现在该做多还是做空？',
    desc: '看一条 50 根 K 线的平均线。K 线的「身子」整个站到线上方，这段时间只做多；整个掉到线下方，只做空。'
        + '\n价格贴着线来回蹭、身子没真正穿过去，就不开仓（算是没方向）。',
    params: [],
  },
  {
    no: '②', id: 'ha', name: 'HA 形态',
    q: '方向是不是真的走稳了？',
    desc: '同一个方向再确认一次，防止在线刚拐弯、还没走稳的时候冲进去。'
        + '\n用「平均K线」（把开高低收四个价平滑过，影线噪音少）看：平滑后的收盘要站在线外侧，并且是朝那个方向走的，才算数。',
    params: [],
  },
  {
    no: '③', id: 'key', name: '关键K线力度',
    q: '这根 K 线有没有劲？',
    desc: '三个条件同时满足才算「有劲」，这是现在唯一的标准（2026-11-12 取消了原来的宽松档）：\n'
        + '① 身子够大：K 线实体至少等于平时一根的平均波动幅度\n'
        + '② 收得够边：收盘价落在最高／最低的最边上一成里\n'
        + '③ 不是长影线：实体占整根八成以上（上下影线加起来不超过两成）\n'
        + '这是五道闸里最窄的一道，绝大部分信号都卡在这里。',
    params: [
      { k: 'key_bar', label: '启用', hint: '关掉 = 这道闸不查了', type: 'bool' },
      { k: 'key_mult', label: '身子倍数', hint: 'K线身子要比平时大几倍。1.0 = 跟平时差不多大就行；调大更严格、开仓更少', type: 'num', step: 0.1, min: 0 },
      { k: 'key_bodyfrac', label: '身子占比', hint: '实体占整根的比例。0.8 = 身子得占八成以上，上下影线加起来不能超过两成', type: 'num', step: 0.01, min: 0, max: 1 },
      { k: 'key_closepos', label: '收在哪', hint: '收盘要落在最边上多少。0.8 = 收在最靠边的那两成里（做多收在高处、做空收在低处）', type: 'num', step: 0.01, min: 0, max: 1 },
    ],
  },
  // ★ 2026-11-12：第④道 BBW 整块已删除（浩哥指令）。六道闸 → 五道闸。
  {
    no: '④', id: 'risk', name: '止损距离',
    q: '这一单的止损，是不是太近或者太远？',
    desc: '1R = 买入价到止损价的距离，也就是这一单最多亏多少钱。这道闸要求它别太近、也别太远。\n'
        + '· 太近（不到 0.6%）：随便一点小抖动就被扫出去，而且手续费占掉一大块\n'
        + '· 太远（超过 5%）：价格根本走不到 2.5 倍的止损距离，白拿着熬\n'
        + '· 太近修正：万一条条线算出来比 0.5% 还近，就改用更远的那条线当止损。',
    params: [
      { k: 'min_stop_dist_pct', label: '最窄', hint: '止损距离小于这个百分比就干脆不开仓；单位 %', type: 'num', step: 0.1, min: 0 },
      { k: 'max_risk_pct', label: '最宽', hint: '止损距离大于这个百分比就不开仓；单位 %', type: 'num', step: 0.1, min: 0 },
      { k: 'dual_stop_min_pct', label: '太近就换', hint: '算出来的止损比这个还近时，改用更远的那条线；单位 %', type: 'num', step: 0.1, min: 0 },
    ],
  },
  {
    no: '⑤', id: 'eff', name: '趋势效率 EFF',
    q: '现在是在走趋势，还是在原地震荡？',
    desc: '看最近 48 根 K 线：净挪动了多远 ÷ 一路上拐来拐去加起来的距离。\n'
        + '· 一条直线涨过去，这个数接近 1（纯趋势）\n'
        + '· 原地来回晃，这个数接近 0（纯震荡）\n'
        + '够 0.3 才算有趋势。震荡里做单，两边都会挨打。',
    params: [
      { k: 'eff_min_signal', label: '门槛', hint: '单边度要到多少才算趋势。0.3 = 要 0.3；0 = 不管这道闸', type: 'num', step: 0.01, min: 0, max: 1 },
      { k: 'eff_win_signal', label: '看多远', hint: '往回看多少根 K 线来算。48 根 = 12 小时', type: 'num', step: 1, min: 0 },
    ],
  },
]

// 止损价格怎么定 —— 不占五道闸的位置，但直接决定第④道闸那个百分比
const STOP_ALGO = [
  { k: 'stop_key', label: '关键K线端点',
    hint: '止损就放在信号那根 K 线的最低点（做多）／最高点（做空）。跌破它，说明这根 K 线的力量已经被否掉了',
    type: 'bool' },
  { k: 'stop_key_atr_buf', label: '再往外让一点',
    hint: '在上面那个点之外再挪 N 倍「平均波动幅度」，专门躲插针。0.2 = 挪 0.2 倍；0 = 不挪',
    type: 'num', step: 0.05, min: 0 },
  { k: 'swing_lookback', label: '找前面的拐点',
    hint: '往回看几根 K 线，找前面那个低点（做多）／高点（做空）当候选止损',
    type: 'num', step: 1, min: 1 },
  { k: 'ce_len', label: 'CE 通道粗细用的根数',
    hint: 'CE 通道用几根 K 线算平均波动。1 = 只用当前这根',
    type: 'num', step: 1, min: 1 },
  { k: 'ce_mult', label: 'CE 通道往外扩',
    hint: 'CE 通道在价格外面再扩几倍平均波动。2.0 = 扩 2 倍',
    type: 'num', step: 0.1, min: 0 },
]

function ParamCell({ def, value, onChange }) {
  if (def.type === 'bool') {
    return (
      <Tooltip title={def.hint}>
        <Switch size="small" checked={!!value} onChange={onChange} />
      </Tooltip>
    )
  }
  return (
    <Tooltip title={def.hint}>
      <InputNumber
        size="small"
        value={value}
        step={def.step}
        min={def.min}
        max={def.max}
        style={{ width: 82 }}
        onChange={onChange}
      />
    </Tooltip>
  )
}

export default function GateTunerPanel({ row, onParamsSave }) {
  const live = React.useMemo(() => ({ ...(row?.params || {}) }), [row?.params])
  const [draft, setDraft] = React.useState(live)
  const [saving, setSaving] = React.useState(false)

  React.useEffect(() => { setDraft({ ...live }) }, [live])

  const dirtyKeys = React.useMemo(
    () => Object.keys(draft).filter((k) => draft[k] !== live[k]),
    [draft, live],
  )
  const dirty = dirtyKeys.length > 0

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }))

  const save = async () => {
    if (!dirty) return
    setSaving(true)
    try {
      const patch = {}
      dirtyKeys.forEach((k) => { patch[k] = draft[k] })
      await onParamsSave(row.id, patch)
      message.success('已保存 ' + dirtyKeys.length + ' 项，下一轮扫描生效，不用重启')
    } catch (e) {
      message.error('保存失败：' + e.message)
    } finally {
      setSaving(false)
    }
  }

  if (!row) {
    return (
      <Card size="small" title="S15 五道闸手动调参">
        <Text type="secondary">没找到 S15 策略，无法调参。</Text>
      </Card>
    )
  }

  const gateRows = GATES.map((g) => ({ ...g, key: g.id }))
  const algoRows = STOP_ALGO.map((a) => ({ ...a, key: a.k }))

  const gateCols = [
    {
      title: '闸', dataIndex: 'no', width: 38, align: 'center',
      render: (v) => <span style={{ fontWeight: 600 }}>{v}</span>,
    },
    {
      title: '闸门', dataIndex: 'name', width: 116,
      render: (v) => (
        <span className="gate-tuner-name" style={{ fontWeight: 600 }}>{v}</span>
      ),
    },
    {
      title: '这道闸在问什么 · 参数怎么读', key: 'body',
      render: (_, r) => (
        <div className="gate-tuner-body">
          <div className="gate-tuner-q"
               style={{ fontSize: 12.5, fontWeight: 600, color: '#1a5fb4' }}>
            {r.q}
          </div>
          <div className="gate-tuner-desc"
               style={{ fontSize: 11.5, lineHeight: 1.6, marginTop: 3, whiteSpace: 'pre-line',
                        color: '#8c8c8c' }}>
            {r.desc}
          </div>
          <div className="gate-tuner-params" style={{ marginTop: r.params.length ? 8 : 0 }}>
            {r.params.length ? (
              <Space wrap size={[14, 6]}>
                {r.params.map((def) => (
                  <span key={def.k} className="gate-tuner-param"
                        style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                    <Tooltip title={def.hint}>
                      <Text style={{ fontSize: 11, whiteSpace: 'nowrap', borderBottom: '1px dashed #bbb' }}>
                        {def.label}
                      </Text>
                    </Tooltip>
                    {draft[def.k] === undefined ? (
                      <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现输入框；现在存进去代码也不认">
                        <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>后台未启用</Tag>
                      </Tooltip>
                    ) : (
                      <ParamCell def={def} value={draft[def.k]} onChange={(v) => set(def.k, v)} />
                    )}
                  </span>
                ))}
              </Space>
            ) : (
              <Text type="secondary" style={{ fontSize: 11 }}>代码固定 · 没有可调参数</Text>
            )}
          </div>
        </div>
      ),
    },
  ]

  return (
    <Card
      size="small"
      className="gate-tuner-card"
      style={{ marginTop: 12 }}
      title={
        <Space size={8} wrap>
          <span>S15 五道闸手动调参</span>
          <Tag color="blue" style={{ fontSize: 11 }}>{row.id}</Tag>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
            鼠标停在任何参数名上，会弹出这个参数怎么读
          </Text>
        </Space>
      }
      extra={
        <Space size={8}>
          {dirty && <Tag color="orange" style={{ fontSize: 11 }}>未保存 {dirtyKeys.length} 项</Tag>}
          <Button size="small" icon={<ReloadOutlined />} disabled={!dirty || saving}
                  onClick={() => setDraft({ ...live })}>
            撤销
          </Button>
          <Button size="small" type="primary" icon={<SaveOutlined />}
                  disabled={!dirty || saving} loading={saving} onClick={save}>
            保存参数
          </Button>
        </Space>
      }
    >
      <Table
        className="gate-tuner-table"
        rowKey="key"
        dataSource={gateRows}
        pagination={false}
        size="small"
        bordered
        columns={gateCols}
      />

      <div className="gate-tuner-algo-head" style={{ marginTop: 14, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 12.5 }}>止损价是怎么算出来的</Text>
        <Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
          不占五道闸的位置，但它决定第④道闸那个百分比，所以放这里一起调
        </Text>
      </div>

      <Table
        className="gate-tuner-algo"
        rowKey="key"
        dataSource={algoRows}
        pagination={false}
        size="small"
        bordered
        columns={[
          { title: '参数', dataIndex: 'label', width: 128, render: (v) => (
            <span style={{ fontSize: 12 }}>{v}</span>
          ) },
          {
            title: '这是干什么的', dataIndex: 'hint',
            render: (v) => (
              <Text type="secondary" style={{ fontSize: 11.5, whiteSpace: 'normal',
                                              display: 'block', lineHeight: 1.5 }}>{v}</Text>
            ),
          },
          {
            title: '当前值', width: 96, align: 'center',
            render: (_, r) => (
              draft[r.k] === undefined ? (
                <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现输入框">
                  <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>未启用</Tag>
                </Tooltip>
              ) : (
                <ParamCell def={r} value={draft[r.k]} onChange={(v) => set(r.k, v)} />
              )
            ),
          },
        ]}
      />

      <div style={{ marginTop: 10 }}>
        <Text type="secondary" style={{ fontSize: 11, lineHeight: 1.55 }}>
          表里所有的「当前值」都是直接从后台设置文件读的，页面不存任何固定数字。
          改完点保存，写回同一个文件，正在跑的策略下一轮（60 秒内）就会用新值，不用重启服务。
        </Text>
      </div>
    </Card>
  )
}
