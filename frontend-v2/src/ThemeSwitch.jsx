import React from 'react'
import { Select } from 'antd'

/**
 * 主题切换：4 套视觉
 *   dark       玫红深色（原始战术大屏）
 *   paper      纸面工程图
 *   mac-dark   MAC 极简 · 深
 *   mac-light  MAC 极简 · 浅
 *
 * 只换视觉，不影响任何功能。选择记在 localStorage.theme，刷新后保持。
 */
const KEY = 'quantbot.theme'

export const THEMES = [
  { value: 'dark', label: '玫红深色' },
  { value: 'paper', label: '纸面' },
  { value: 'mac-dark', label: 'MAC 深色' },
  { value: 'mac-light', label: 'MAC 浅色' },
]

export function getTheme() {
  try {
    return localStorage.getItem(KEY) || 'dark'
  } catch (e) {
    return 'dark'
  }
}

export function applyTheme(mode) {
  const root = document.documentElement
  if (!mode || mode === 'dark') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', mode)
  try { localStorage.setItem(KEY, mode || 'dark') } catch (e) { /* 隐私模式忽略 */ }
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
  const mode = useThemeMode()
  return (
    <Select
      size="small"
      value={mode}
      onChange={applyTheme}
      options={THEMES}
      style={{ width: 104 }}
      popupMatchSelectWidth={132}
      aria-label="主题"
    />
  )
}
