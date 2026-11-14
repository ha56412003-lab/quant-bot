import React from 'react'
import { Card, Table, Switch, InputNumber, Button, Tag, Typography, Space, message, Tooltip, Modal } from 'antd'
import { ReloadOutlined, SaveOutlined, RollbackOutlined } from '@ant-design/icons'

const { Text } = Typography

/**
 * S15 六道闸手动调参
 * ─────────────────────────────────────────────────────────────
 * 位置：策略库页面 S15 策略表格下方
 * 数据源：全部从 row.params（后端 strategies.yaml 实时值）读，前端不写死任何门槛
 * 保存：走 onParamsSave(row.id, patch) → api.strategyConfig → strategies.yaml
 *       ★ 网页改参数即时生效，不用重启（config.load_strategies() 每轮重读，TICK_SEC=60）
 *
 * ★ 闸门顺序与「光闸面板」(ActivityTabs) 的 6 列严格一致：
 *     时段 → HA → 关键K → BBW → 止损距离 → EFF
 *   偏离闸在 VS15-1.1 已从后端物理删除，VS15-1.2 把面板这一行也删了（不再留灰显占位）。
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
    desc: '三个条件同时满足才算「有劲」：\n'
        + '① 身子够大：K 线实体至少等于平时一根的平均波动幅度\n'
        + '② 收得够边：收盘价落在最高／最低的那一小块，不是冲出去又拉回来\n'
        + '③ 不是长影线：实体占整根（连影线）的一半以上\n'
        + '这是六道闸里最窄的一道，八成以上的信号都卡在这里。',
    params: [
      { k: 'key_bar', label: '启用', hint: '关掉 = 这道闸不查了', type: 'bool' },
      { k: 'key_mult', label: '身子倍数', hint: 'K线身子要比平时大几倍。1.0 = 跟平时差不多大就行；调大更严格、开仓更少', type: 'num', step: 0.1, min: 0 },
      { k: 'key_bodyfrac', label: '身子占比', hint: '实体占整根的比例。0.5 = 身子得占一半以上，影线不能太长', type: 'num', step: 0.01, min: 0, max: 1 },
      { k: 'key_closepos', label: '收在哪', hint: '收盘要落在最边上多少。0.66 = 收在最靠边的那三分之一里（做多收在高处、做空收在低处）', type: 'num', step: 0.01, min: 0, max: 1 },
    ],
  },
  {
    no: '④', id: 'bbw', name: 'BBW 布林带宽',
    q: '行情热不热闹？',
    desc: '要求「行情正在放大，不是在缩着磨蹭」。\n'
        + '布林带是价格波动的上下通道，通道越宽说明波动越大。拿现在的宽度跟过去 100 根比，要排进最宽的那一部分才做。'
        + '\n行情缩着不动的时候开仓，最容易两头挨打。'
        + '\n\n★ 顶级K线免检（2026-11-12 加）：如果一根K线本身就是「身子够猛 + 收在最边上 + 几乎没影线」，'
        + '说明爆发已经开始，这时候再等布林带变宽就晚了 —— 实测会晚 2~3 小时进场、同样的行情少赚三成。'
        + '这种K线直接放行，不等这道闸。下面三个数就是「什么样算顶级」。',
    params: [
      { k: 'bbw_pct_min', label: '要多宽', hint: '排进前面百分之几。70 = 要比过去 100 根里 70% 的时候都宽。0 = 不管这道闸', type: 'num', step: 1, min: 0, max: 100 },
      { k: 'bbw_exempt_mult', label: '免检·身子', hint: 'K线身子比平时大几倍就免检。1.0 = 跟平时一样大就行；0 = 关闭免检（恢复老规矩，所有单都要等布林带变宽）', type: 'num', step: 0.1, min: 0 },
      { k: 'bbw_exempt_closepos', label: '免检·收盘位', hint: '收盘要落在最边上多少才免检。0.8 = 收在最靠边的那两成里（做多收在高处、做空收在低处）', type: 'num', step: 0.01, min: 0, max: 1 },
      { k: 'bbw_exempt_bodyfrac', label: '免检·实体占比', hint: '实体占整根多少才免检。0.8 = 上下影线加起来不能超过两成', type: 'num', step: 0.01, min: 0, max: 1 },
    ],
  },
  {
    no: '⑤', id: 'risk', name: '止损距离',
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
    no: '⑥', id: 'eff', name: '趋势效率 EFF',
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

// 止损价格怎么定 —— 不占六道闸的位置，但直接决定第⑤道闸那个百分比
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

// 赚到几个 R 就动手 —— 出场三档（2026-11-12 浩哥要求加到界面）
// ★ 这三个数决定「什么时候落袋」，是全部出场逻辑的骨架。
//   改了这三个数，等于改了整个策略的盈亏结构，比改入场参数影响更大。
const EXIT_R = [
  { k: 'be_at_r', label: '保本移损',
    hint: '赚到几个 R 就把止损挪到成本价 —— 从此这一单最差是白干，不会再亏。'
        + 'R = 买入价到止损价的距离，也就是这一单本来打算亏多少。1.5 = 赚到 1.5 倍那个距离',
    type: 'num', step: 0.1, min: 0 },
  { k: 'half_at_r', label: '平掉一半',
    hint: '赚到几个 R 先平一半落袋，剩一半继续跑。'
        + '★ 必须小于「全部平仓」那个数，否则全平先触发、半平永远轮不到',
    type: 'num', step: 0.1, min: 0 },
  { k: 'full_at_r', label: '全部平仓',
    hint: '赚到几个 R 剩下的全部平掉，不等它回头。'
        + '★ 必须大于「平掉一半」那个数',
    type: 'num', step: 0.1, min: 0 },
]

// 右侧止盈要求「反向K线」也得够硬 —— 2026-11-12 夜从入场三键里拆出来独立
// ★ 为什么要拆：以前它俩共用一个旋钮，把入场放宽（key_closepos 0.66→0.6）
//   会顺带把止盈也放宽 → 更容易触发止盈 → 利润被提前截断。方向相反的两件事。
const RIGHT_EXIT = [
  { k: 'right_key_mult', label: '力度',
    hint: '右侧止盈要求反向K线的实体至少是平均波动的几倍。'
        + '这个数越大，止盈越难触发、拿得越久；越小越容易兑现。'
        + '★ 只影响「什么时候止盈」，不影响开仓',
    type: 'num', step: 0.1, min: 0 },
  { k: 'right_key_closepos', label: '收盘位置',
    hint: '反向K线的收盘要落在当根K线的多极端处（0=最差，1=最好）。'
        + '★ 只影响止盈，不影响开仓',
    type: 'num', step: 0.02, min: 0, max: 1 },
  { k: 'right_key_bodyfrac', label: '实体占比',
    hint: '反向K线实体占整根（含上下影线）的比例，越大越干脆。'
        + '★ 只影响止盈，不影响开仓',
    type: 'num', step: 0.05, min: 0, max: 1 },
]

// ★ 出场 · 第④条「时段翻转平」（VS15-1.5 新增，默认关闭）
//   后端只有一个数字键 stop_key_atr_buf_tight：0 = 关，>0 = 往外让几倍波动幅度后撤出。
//   界面上做成「开关 + 一个数」，开关打开时填 ON_PAD，关掉时写回 0 —— 不新增后端键。
//   ON_PAD 只是「打开时先给个数」，不是策略门槛，真正生效的值永远是后台读回来的那个数。
const X3_ON_PAD = 0.2

const fmtVal = (v) => (typeof v === 'boolean' ? (v ? '开' : '关') : v)

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
  // ★ 只在「后台值真的变了」时重置草稿。外层每 15 秒轮询一次，每次都会换一个新的 params
  //   对象；若直接依赖对象本身，用户改到一半会被静默清空 —— 实测点开 X3 开关后等一轮
  //   轮询，开关自己退回「关」、「未保存 N 项」也一起消失，看起来像点了没反应。
  const liveKey = React.useMemo(() => JSON.stringify(live), [live])
  const [draft, setDraft] = React.useState(live)
  const [saving, setSaving] = React.useState(false)

  React.useEffect(() => { setDraft({ ...live }) }, [liveKey])

  const dirtyKeys = React.useMemo(
    () => Object.keys(draft).filter((k) => draft[k] !== live[k]),
    [draft, live],
  )
  const dirty = dirtyKeys.length > 0

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }))

  // ★ 定版默认值从后端读（row.defaults），前端不写死任何数字。
  //   以后改定版值只改 config.py 的 _STRAT_PARAM_DEFAULTS 一处。
  const DEFAULTS = React.useMemo(() => ({ ...(row?.defaults || {}) }), [row?.defaults])

  // 页面上真正有输入框的键。★ 必须限定范围：
  //   draft 里其实是全部 35 个参数（含一堆页面没给框的），如果只按「跟定版值不同」
  //   就收进 diff，那么「恢复默认」会把用户用改文件方式调过的参数也一起改回去 ——
  //   按钮叫「恢复默认」，用户预期是恢复**这个面板上**的，不是偷偷动别处。
  const UI_KEYS = React.useMemo(() => {
    const s = new Set()
    // ★ GATES 是两层结构（每道闸有个 params 数组），另外三个常量是扁平数组 ——
    //   这里写错过一次：用 g.fields 取不到东西，导致六道闸上的 4 个参数
    //   被「恢复默认」无视（按钮显示 1 项，实际该是 5 项）。
    GATES.forEach((g) => (g.params || []).forEach((f) => s.add(f.k)))
    STOP_ALGO.forEach((f) => s.add(f.k))
    EXIT_R.forEach((f) => s.add(f.k))
    // ★ 第④条「时段翻转平」只有一个数字键（0=关），也必须进这个集合，
    //   否则「恢复默认」不会把它关回去 —— 用户开了之后再点恢复，会以为退回定版了。
    s.add('stop_key_atr_buf_tight')
    RIGHT_EXIT.forEach((f) => s.add(f.k))
    return s
  }, [])

  // 当前值跟定版值不一样的键（只在页面有的键里比）
  const diffFromDefaults = React.useMemo(() => {
    const out = {}
    UI_KEYS.forEach((k) => {
      if (draft[k] !== undefined && DEFAULTS[k] !== undefined && draft[k] !== DEFAULTS[k]) {
        out[k] = DEFAULTS[k]
      }
    })
    return out
  }, [draft, DEFAULTS, UI_KEYS])

  const resetToDefaults = () => {
    const keys = Object.keys(diffFromDefaults)
    if (!keys.length) {
      message.info('当前所有参数已经是核对过的定版值，不用恢复')
      return
    }
    const lines = keys.map((k) => `${k}：${fmtVal(draft[k])} → ${fmtVal(DEFAULTS[k])}`)
    Modal.confirm({
      title: `恢复定版默认（${keys.length} 项）`,
      width: 520,
      content: (
        <div style={{ fontSize: 12, lineHeight: 1.7 }}>
          <div style={{ marginBottom: 8, color: '#c4182c' }}>
            这是把参数整体退回「核对过的那一版」，会覆盖你现在所有的改动。
          </div>
          <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 11.5,
                        background: '#fafafa', padding: '8px 10px', borderRadius: 4,
                        maxHeight: 260, overflow: 'auto' }}>
            {lines.map((s) => <div key={s}>{s}</div>)}
          </div>
        </div>
      ),
      okText: '确认恢复并保存',
      cancelText: '先不恢复',
      onOk: async () => {
        setSaving(true)
        try {
          await onParamsSave(row.id, { ...diffFromDefaults })
          setDraft((d) => ({ ...d, ...diffFromDefaults }))
          message.success('已恢复 ' + keys.length + ' 项定版值，下一轮扫描生效，不用重启')
        } catch (e) {
          message.error('恢复失败：' + e.message)
        } finally {
          setSaving(false)
        }
      },
    })
  }

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
      <Card size="small" title="S15 六道闸手动调参">
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
                      <>
                        <ParamCell def={def} value={draft[def.k]} onChange={(v) => set(def.k, v)} />
                        {DEFAULTS[def.k] !== undefined && draft[def.k] !== DEFAULTS[def.k] && (
                          <Tooltip title={'定版值是 ' + fmtVal(DEFAULTS[def.k])
                            + '。点右上角「恢复默认」可以整体退回定版值'}>
                            <Text style={{ fontSize: 10, color: '#d46b08', whiteSpace: 'nowrap' }}>
                              默认{fmtVal(DEFAULTS[def.k])}
                            </Text>
                          </Tooltip>
                        )}
                      </>
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
          <span>S15 六道闸手动调参</span>
          <Tag color="blue" style={{ fontSize: 11 }}>{row.id}</Tag>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
            鼠标停在任何参数名上，会弹出这个参数怎么读
          </Text>
        </Space>
      }
      extra={
        <Space size={8}>
          {dirty && <Tag color="orange" style={{ fontSize: 11 }}>未保存 {dirtyKeys.length} 项</Tag>}
          <Tooltip title="把所有参数退回 2026-11-12 核对过的定版值（会覆盖当前所有改动）">
            <Button size="small" danger icon={<RollbackOutlined />}
                    disabled={saving || !Object.keys(diffFromDefaults).length}
                    onClick={resetToDefaults}>
              恢复默认{Object.keys(diffFromDefaults).length ? ` (${Object.keys(diffFromDefaults).length})` : ''}
            </Button>
          </Tooltip>
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
          不占六道闸的位置，但它决定第⑤道闸那个百分比，所以放这里一起调
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
            title: '当前值', width: 118, align: 'center',
            render: (_, r) => (
              draft[r.k] === undefined ? (
                <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现输入框">
                  <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>未启用</Tag>
                </Tooltip>
              ) : (
                <div>
                  <ParamCell def={r} value={draft[r.k]} onChange={(v) => set(r.k, v)} />
                  {DEFAULTS[r.k] !== undefined && draft[r.k] !== DEFAULTS[r.k] && (
                    <div style={{ marginTop: 2 }}>
                      <Tooltip title={'定版值是 ' + fmtVal(DEFAULTS[r.k])}>
                        <Text style={{ fontSize: 10, color: '#d46b08' }}>
                          默认{fmtVal(DEFAULTS[r.k])}
                        </Text>
                      </Tooltip>
                    </div>
                  )}
                </div>
              )
            ),
          },
        ]}
      />

      <div className="gate-tuner-exit-head" style={{ marginTop: 14, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 12.5 }}>赚到多少就落袋 · 出场三档</Text>
        <Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
          这三个数决定什么时候收手，比入场参数影响更大 —— 改之前先想清楚
        </Text>
      </div>

      <Table
        className="gate-tuner-exit"
        rowKey="k"
        dataSource={EXIT_R.map((x) => ({ ...x, key: x.k }))}
        pagination={false}
        size="small"
        bordered
        columns={[
          { title: '档位', dataIndex: 'label', width: 128, render: (v) => (
            <span style={{ fontSize: 12, fontWeight: 600 }}>{v}</span>
          ) },
          {
            title: '这是干什么的', dataIndex: 'hint',
            render: (v) => (
              <Text type="secondary" style={{ fontSize: 11.5, whiteSpace: 'normal',
                                              display: 'block', lineHeight: 1.5 }}>{v}</Text>
            ),
          },
          {
            title: '当前值', width: 118, align: 'center',
            render: (_, r) => (
              draft[r.k] === undefined ? (
                <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现输入框">
                  <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>未启用</Tag>
                </Tooltip>
              ) : (
                <div>
                  <ParamCell def={r} value={draft[r.k]} onChange={(v) => set(r.k, v)} />
                  {DEFAULTS[r.k] !== undefined && draft[r.k] !== DEFAULTS[r.k] && (
                    <div style={{ marginTop: 2 }}>
                      <Tooltip title={'定版值是 ' + fmtVal(DEFAULTS[r.k])}>
                        <Text style={{ fontSize: 10, color: '#d46b08' }}>
                          默认{fmtVal(DEFAULTS[r.k])}
                        </Text>
                      </Tooltip>
                    </div>
                  )}
                </div>
              )
            ),
          },
        ]}
      />

      {/* ── 出场 · 第④条「时段翻转平」（VS15-1.5 新增，默认关闭）──────────── */}
      <div className="gate-tuner-x3-head" style={{ marginTop: 14, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 12.5 }}>出场 · 时段翻转平（第④条）</Text>
        <Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
          默认关闭。开仓后一直没跑出利润、行情又翻到反面时，别等原止损，直接撤
        </Text>
      </div>

      <div className="gate-tuner-x3"
           style={{ border: '1px solid #f0f0f0', borderRadius: 4, padding: '10px 12px' }}>

        <Space size={[18, 10]} wrap align="center">
          <span className="gate-tuner-param"
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <Tooltip title="打开 = 这条出场条件生效；关掉 = 完全不参与，等于现在线上的状态">
              <Text style={{ fontSize: 11.5, whiteSpace: 'nowrap', borderBottom: '1px dashed #bbb' }}>
                启用
              </Text>
            </Tooltip>
            {draft.stop_key_atr_buf_tight === undefined ? (
              <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现开关">
                <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>后台未启用</Tag>
              </Tooltip>
            ) : (
              <Switch size="small"
                      checked={Number(draft.stop_key_atr_buf_tight) > 0}
                      disabled={saving}
                      onChange={(v) => set('stop_key_atr_buf_tight', v ? X3_ON_PAD : 0)} />
            )}
          </span>

          <span className="gate-tuner-param"
                style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <Tooltip title={'撤出的位置 = 信号那根K线的高点（做多）／低点（空单）+ 这个数 × 平均波动幅度。'
              + ' 0.2 = 再让 0.2 倍波动，专门躲插针；0 = 贴着那根K线的端点走'}>
              <Text style={{ fontSize: 11, whiteSpace: 'nowrap', borderBottom: '1px dashed #bbb' }}>
                再往外让
              </Text>
            </Tooltip>
            {draft.stop_key_atr_buf_tight === undefined ? null : (
              <InputNumber
                size="small"
                value={draft.stop_key_atr_buf_tight}
                step={0.05}
                min={0}
                max={5}
                style={{ width: 82 }}
                disabled={saving || !(Number(draft.stop_key_atr_buf_tight) > 0)}
                onChange={(v) => set('stop_key_atr_buf_tight', v === null ? 0 : v)}
              />
            )}
          </span>

          <Text type="secondary" style={{ fontSize: 11 }}>
            峰顶门槛：赚到 {fmtVal(draft.be_at_r)}R 之前才算「没跑出来」（跟上面「保本移损」同一个数，改那边这里跟着变）
          </Text>
        </Space>

        <div className="gate-tuner-desc"
             style={{ fontSize: 11.5, lineHeight: 1.65, marginTop: 8, whiteSpace: 'pre-line',
                      color: '#8c8c8c' }}>
          {'这条在问什么：开进去以后价格一直没往我这边走（最高只赚到不到 '}
          {fmtVal(draft.be_at_r)}
          {'R），行情却已经翻到反面了 —— 说明这单已经错了。别等价格慢慢磨到原止损，当场按市价撤掉。\n'
           + '★ 什么时候不算数：还没翻、或者峰顶已经超过那个 R（说明这单是对的，交给保本和止盈去管）。\n'
           + '★ 回测结论（2026-11-14，头部10币 + 50币双样本，往外让 0.1/0.2/0.3/0.5 四档）：全都不如关着。'
           + '它把「大亏」变成「小亏」，同时也把「大赚」砍成「小亏」，净值基本不变；唯一实打实的好处是最大回撤降 16%。'
           + '所以定版值是 0（关）。想拿回撤换收益的时候再开。'}
        </div>
      </div>

      <div className="gate-tuner-rexit-head" style={{ marginTop: 14, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 12.5 }}>右侧止盈要的证据有多硬</Text>
        <Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
          只影响「什么时候止盈离场」，和开仓完全不搭界
        </Text>
      </div>

      <Table
        className="gate-tuner-rexit"
        rowKey="k"
        dataSource={RIGHT_EXIT.map((x) => ({ ...x, key: x.k }))}
        pagination={false}
        size="small"
        bordered
        columns={[
          { title: '门槛', dataIndex: 'label', width: 108, render: (v) => (
            <span style={{ fontSize: 12, fontWeight: 600 }}>{v}</span>
          ) },
          {
            title: '这是干什么的', dataIndex: 'hint',
            render: (v) => (
              <Text type="secondary" style={{ fontSize: 11.5, whiteSpace: 'normal',
                                              display: 'block', lineHeight: 1.5 }}>{v}</Text>
            ),
          },
          {
            title: '当前值', width: 118, align: 'center',
            render: (_, r) => (
              draft[r.k] === undefined ? (
                <Tooltip title="后台配置里还没有这个键。先上线对应代码，这里才会出现输入框">
                  <Tag color="default" style={{ fontSize: 10.5, margin: 0 }}>未启用</Tag>
                </Tooltip>
              ) : (
                <div>
                  <ParamCell def={r} value={draft[r.k]} onChange={(v) => set(r.k, v)} />
                  {DEFAULTS[r.k] !== undefined && draft[r.k] !== DEFAULTS[r.k] && (
                    <div style={{ marginTop: 2 }}>
                      <Tooltip title={'定版值是 ' + fmtVal(DEFAULTS[r.k])}>
                        <Text style={{ fontSize: 10, color: '#d46b08' }}>
                          默认{fmtVal(DEFAULTS[r.k])}
                        </Text>
                      </Tooltip>
                    </div>
                  )}
                </div>
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
