import './v2.css'
import './paper.css'
import './mac.css'
import { initTheme, useThemeMode } from './ThemeSwitch'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, theme as antdTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import App from './App'
import { I18nProvider, useI18n } from './i18n'

/* ── 玫红深色主题（原来的，一字未改） ── */
const DARK_THEME = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: '#c1207e',
    colorInfo: '#45e6f5',
    colorSuccess: 'var(--green, #3fa86a)',
    colorError: 'var(--red, #d4595e)',
    colorWarning: '#ffb020',
    colorLink: '#45e6f5',
    colorBgBase: '#07080d',
    colorBgContainer: '#0b0c12',
    colorBgElevated: '#10121a',
    colorBorder: 'rgba(193,32,126,0.30)',
    colorBorderSecondary: 'rgba(193,32,126,0.15)',
    colorText: '#eef0f7',
    colorTextSecondary: '#b8bccb',
    colorTextTertiary: '#73788e',
    borderRadius: 3,
    borderRadiusLG: 4,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: "'Cascadia Mono', 'JetBrains Mono', Consolas, 'Microsoft YaHei', 'PingFang SC', monospace",
  },
  components: {
    Card: { colorBgContainer: 'rgba(11,12,18,0.92)', headerBg: 'rgba(16,18,26,0.75)' },
    Table: {
      headerBg: '#10121a',
      headerColor: '#45e6f5',
      rowHoverBg: 'rgba(31,168,196,0.07)',
      borderColor: 'rgba(193,32,126,0.12)',
    },
    Button: { primaryShadow: '0 0 10px rgba(193,32,126,0.35)' },
    Tag: { defaultBg: 'rgba(31,168,196,0.1)' },
    Statistic: { contentFontSize: 22 },
  },
}

/* ── 纸面主题：浅色底 + 墨黑强调 + 压深的辅助色 ──
   注意 antd 的 darkAlgorithm 是 JS 层决定组件底色的，
   CSS 覆盖不彻底，所以必须在这里一起切 defaultAlgorithm。 */
const PAPER_THEME = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#151515',
    colorInfo: '#2b6d80',
    colorSuccess: '#1f7a4d',
    colorError: '#b8303c',
    colorWarning: '#8a6a12',
    colorLink: '#2b6d80',
    colorBgBase: '#eeede8',
    colorBgContainer: '#f6f5f1',
    colorBgElevated: '#fbfaf7',
    colorBorder: 'rgba(21,21,21,0.22)',
    colorBorderSecondary: 'rgba(21,21,21,0.12)',
    colorText: '#151515',
    colorTextSecondary: '#3a3a36',
    colorTextTertiary: '#5f5e58',
    borderRadius: 3,
    borderRadiusLG: 4,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: "'Cascadia Mono', 'JetBrains Mono', Consolas, 'Microsoft YaHei', 'PingFang SC', monospace",
  },
  components: {
    Card: { colorBgContainer: '#f6f5f1', headerBg: '#e9e8e2' },
    Table: {
      headerBg: '#e4e2dc',
      headerColor: '#3a3a36',
      rowHoverBg: 'rgba(21,21,21,0.05)',
      borderColor: 'rgba(21,21,21,0.12)',
    },
    Button: { primaryShadow: 'none' },
    Tag: { defaultBg: 'rgba(21,21,21,0.06)' },
    Statistic: { contentFontSize: 22 },
  },
}


const MAC_FONT_STACK = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Noto Sans SC", sans-serif'

/* ── MAC 极简 · 深 ──
   antd 的组件底色由 algorithm 决定，CSS 覆盖不彻底，
   所以浅色/深色必须在这里跟着 data-theme 一起切。 */
const MAC_DARK_THEME = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: '#e58bb4',
    colorInfo: '#e58bb4',
    colorSuccess: '#4ade80',
    colorError: '#ff6b6b',
    colorWarning: '#e0a352',
    colorLink: '#e58bb4',
    colorBgBase: '#0e0d11',
    colorBgContainer: '#17161b',
    colorBgElevated: '#232129',
    colorBorder: '#322f3c',
    colorBorderSecondary: '#262430',
    colorText: '#f5f3f7',
    colorTextSecondary: '#cbc5d3',
    colorTextTertiary: '#a9a2b4',
    borderRadius: 4,
    borderRadiusLG: 6,
    borderRadiusSM: 3,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: MAC_FONT_STACK,
  },
  components: {
    Card: { colorBgContainer: '#17161b', headerBg: 'transparent' },
    Table: {
      headerBg: '#1c1b21',
      headerColor: '#a9a2b4',
      rowHoverBg: '#232129',
      borderColor: '#262430',
    },
    Button: { primaryShadow: 'none' },
    Tag: { defaultBg: '#232129' },
    Statistic: { contentFontSize: 22 },
  },
}

/* ── MAC 极简 · 浅 ── */
const MAC_LIGHT_THEME = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#b71c5a',
    colorInfo: '#b71c5a',
    colorSuccess: '#2e7d32',
    colorError: '#c62828',
    colorWarning: '#a87000',
    colorLink: '#b71c5a',
    colorBgBase: '#f7f3f7',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: '#e6dae6',
    colorBorderSecondary: '#f0e7f0',
    colorText: '#1c1b1f',
    colorTextSecondary: '#4a4550',
    colorTextTertiary: '#6e6e73',
    borderRadius: 4,
    borderRadiusLG: 6,
    borderRadiusSM: 3,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: MAC_FONT_STACK,
  },
  components: {
    Card: { colorBgContainer: '#ffffff', headerBg: 'transparent' },
    Table: {
      headerBg: '#f7f3f7',
      headerColor: '#6e6e73',
      rowHoverBg: 'rgba(0,0,0,0.03)',
      borderColor: '#f0e7f0',
    },
    Button: { primaryShadow: 'none' },
    Tag: { defaultBg: 'rgba(0,0,0,0.04)' },
    Statistic: { contentFontSize: 22 },
  },
}


function LocaleShell() {
  const { lang } = useI18n()
  const mode = useThemeMode()
  return (
    <ConfigProvider
      locale={lang === 'en' ? enUS : zhCN}
      theme={
        mode === 'paper' ? PAPER_THEME
        : mode === 'mac-dark' ? MAC_DARK_THEME
        : mode === 'mac-light' ? MAC_LIGHT_THEME
        : DARK_THEME
      }
    >
      <App />
    </ConfigProvider>
  )
}

initTheme()

ReactDOM.createRoot(document.getElementById('root')).render(
  <I18nProvider>
    <LocaleShell />
  </I18nProvider>
)
