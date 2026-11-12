import { message } from 'antd'
import { api } from '../api'
import { usePoll } from '../hooks'
import { useI18n } from '../i18n'
import StrategiesPanel from '../panels/StrategiesPanel'
import GateTunerPanel from '../panels/GateTunerPanel'

export default function StrategiesPage() {
  const [strategies, refresh] = usePoll(api.strategies, 15000)
  const [universe] = usePoll(api.universe, 60000)
  const { t } = useI18n()

  const wrap = (fn, okKey) => async (...args) => {
    try { await fn(...args); if (okKey) message.success(t(okKey)); refresh() }
    catch (e) { message.error(`${t('common.opFailed')}: ${e.message}`); throw e }
  }

  const onToggle = async (id, enabled) => {
    try {
      await api.toggleStrategy(id, enabled)
      message.success(`${id} ${enabled ? t('strat.enabledMsg') : t('strat.disabledMsg')}`)
      refresh()
    } catch (e) { message.error(`${t('common.opFailed')}: ${e.message}`) }
  }

  // ★ 七道闸调参面板要拿到 S15 这一行的实时 params（后端 yaml 真值）
  const s15Row = (strategies?.strategies || []).find((r) => String(r.id).startsWith('S15'))

  return (
    <>
      <StrategiesPanel data={strategies} universe={universe?.universe || []}
                       known={universe?.known || []}
                       onToggle={onToggle}
                       onIntervalSave={wrap((id, sec) => api.strategyConfig(id, { scan_interval_sec: sec }), 'strat.intervalSaved')}
                       onSymbolsSave={wrap((id, symbols) => api.strategyConfig(id, { symbols }), 'strat.symbolsSaved')}
                       onParamsSave={wrap((id, params) => api.strategyConfig(id, { params }), 'strat.paramsSaved')}
                       onCreate={wrap((base, name) => api.createStrategy(base, name), 'strat.created')}
                       onDelete={wrap((id) => api.deleteStrategy(id), 'strat.deleted')} />
      <GateTunerPanel row={s15Row}
                      onParamsSave={wrap((id, params) => api.strategyConfig(id, { params }), 'strat.paramsSaved')} />
    </>
  )
}
