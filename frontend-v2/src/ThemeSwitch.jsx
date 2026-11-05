import React from 'react'

/**
 * 背景主题切换：玫红深色 ⇄ 纸面工程图
 *
 * 只切背景（方案 A）：页面底色、网格、扫描线、顶栏跟着换，
 * 卡片/面板/表格保持深色，形成"纸面上贴深色显示板"的效果。
 *
 * 选择记在 localStorage.theme，刷新后保持。
 */
const KEY = 'quantbot.theme'

export function getTheme() {
  try {
    return localStorage.getItem(KEY) || 'dark'
  } catch (e) {
    return 'dark'
  }
}

export function applyTheme(mode) {
  const root = document.documentElement
  if (mode === 'paper') root.setAttribute('data-theme', 'paper')
  else root.removeAttribute('data-theme')
  try { localStorage.setItem(KEY, mode) } catch (e) { /* 隐私模式忽略 */ }
}

/** 挂载时把上次的选择套回去 */
export function initTheme() {
  applyTheme(getTheme())
}

/** 订阅 data-theme，主题变化时触发重渲染（给 antd ConfigProvider 用） */
export function useThemeMode() {
  const [mode, setMode] = React.useState(getTheme)
  React.useEffect(() => {
    const obs = new MutationObserver(() => setMode(getTheme()))
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return mode
}

export default function ThemeSwitch() {
  const [mode, setMode] = React.useState(getTheme)

  const toggle = () => {
    const next = mode === 'paper' ? 'dark' : 'paper'
    setMode(next)
    applyTheme(next)
  }

  const isPaper = mode === 'paper'

  return (
    <button
      onClick={toggle}
      title={isPaper ? '切换到玫红深色背景' : '切换到纸面工程图背景'}
      style={{
        padding: '5px 12px',
        fontSize: 12,
        cursor: 'pointer',
        borderRadius: 3,
        // 在深色顶栏和纸白顶栏下都要看得清，所以配色跟随当前主题
        background: isPaper ? 'rgba(21,21,21,.06)' : 'rgba(180,47,128,.18)',
        color: isPaper ? '#151515' : '#ff7fcb',
        border: '1px solid ' + (isPaper ? '#c4c2ba' : '#ec4fae'),
        letterSpacing: '.02em',
        whiteSpace: 'nowrap',
      }}
    >
      {isPaper ? '◐ 纸面' : '◑ 玫红'}
    </button>
  )
}
