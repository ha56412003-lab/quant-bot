import ThemeSwitch from './ThemeSwitch'
import PaperField from './PaperField'
import { useState } from 'react'
import { api } from './api'
import { usePoll } from './hooks'
import { useI18n } from './i18n'
import { ModeSwitch } from './panels/StatusBar'
import SpiderBoard from './panels/SpiderBoard'
import Dashboard from './pages/Dashboard'
import StrategiesPage from './pages/StrategiesPage'
import FactorsPage from './pages/FactorsPage'
import JournalPage from './pages/JournalPage'
import RiskPage from './pages/RiskPage'
import ConfigPage from './pages/ConfigPage'

const PAGES = [
  ['dashboard', '总览', Dashboard],
  ['strategies', '策略库', StrategiesPage],
  ['factors', '因子库', FactorsPage],
  ['journal', '交易日志', JournalPage],
  ['risk', '风控', RiskPage],
  ['config', '配置', ConfigPage],
]

export default function App() {
  const [page, setPage] = useState(() => {
    const q = new URLSearchParams(window.location.search).get('page')
    return PAGES.some((p) => p[0] === q) ? q : 'dashboard'
  })
  const [status] = usePoll(api.status, 5000)
  const [positions] = usePoll(api.positions, 5000)
  const { t, lang, setLang } = useI18n()

  const nav = PAGES.map(([k, label]) => [k, label])
  const PageComp = (PAGES.find((p) => p[0] === page) || PAGES[0])[2]
  const pos = positions?.positions || []

  if (page !== 'dashboard') {
    return (
      <>
      <PaperField />
      <div style={{ padding: 24, minHeight: '100vh' }}>
        <div style={{ marginBottom: 16, display: 'flex', gap: 8, alignItems: 'center' }}>
          <b style={{ fontSize: 16, color: '#ff7fcb', letterSpacing: '0.1em' }}>AGENT OPS</b>
          {nav.map(([k, label]) => (
            <button key={k} onClick={() => setPage(k)}
              style={{
                padding: '5px 12px', fontSize: 12, cursor: 'pointer',
                background: page === k ? 'rgba(180,47,128,.35)' : 'transparent',
                color: page === k ? '#ff7fcb' : '#73788e',
                border: '1px solid ' + (page === k ? '#ec4fae' : '#262a39'),
                borderRadius: 3,
              }}>{label}</button>
          ))}
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            <ThemeSwitch />
            <ModeSwitch status={status} />
          </span>
        </div>
        <PageComp status={status} />
      </div>
      </>
    )
  }

  return (
    <>
    <PaperField />
    <SpiderBoard status={status} positions={pos} nav={nav}
                 activeNav={page} onNavigate={setPage}>
      <PageComp status={status} />
    </SpiderBoard>
    </>
  )
}
