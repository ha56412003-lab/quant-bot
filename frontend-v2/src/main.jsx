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
    colorInfo: '#1fa8c4',
    colorSuccess: 'var(--green, #3fa86a)',
    colorError: 'var(--red, #d4595e)',
    colorWarning: '#d0a03a',
    colorLink: '#1fa8c4',
    colorBgBase: '#05070a',
    colorBgContainer: '#0e131a',
    colorBgElevated: '#141b24',
    colorBorder: 'rgba(193,32,126,0.30)',
    colorBorderSecondary: 'rgba(193,32,126,0.15)',
    colorText: '#f2f6fa',
    colorTextSecondary: '#8a96a8',
    colorTextTertiary: '#6b7787',
    borderRadius: 3,
    borderRadiusLG: 4,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: "'Cascadia Mono', 'JetBrains Mono', Consolas, 'Microsoft YaHei', 'PingFang SC', monospace",
  },
  components: {
    Card: { colorBgContainer: 'rgba(14,19,26,0.92)', headerBg: 'rgba(20,27,36,0.75)' },
    Table: {
      headerBg: '#101720',
      headerColor: '#7fe3f7',
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
    colorPrimary: '#0a84ff',
    colorInfo: '#0a84ff',
    colorSuccess: '#30d158',
    colorError: '#ff453a',
    colorWarning: '#ff9f0a',
    colorLink: '#0a84ff',
    colorBgBase: '#000000',
    colorBgContainer: '#1c1c1e',
    colorBgElevated: '#2c2c2e',
    colorBorder: 'rgba(255,255,255,0.10)',
    colorBorderSecondary: 'rgba(255,255,255,0.07)',
    colorText: '#f5f5f7',
    colorTextSecondary: '#c7c7cc',
    colorTextTertiary: '#98989d',
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: MAC_FONT_STACK,
  },
  components: {
    Card: { colorBgContainer: '#1c1c1e', headerBg: 'transparent' },
    Table: {
      headerBg: '#232325',
      headerColor: '#98989d',
      rowHoverBg: 'rgba(255,255,255,0.04)',
      borderColor: 'rgba(255,255,255,0.07)',
    },
    Button: { primaryShadow: 'none' },
    Tag: { defaultBg: 'rgba(255,255,255,0.06)' },
    Statistic: { contentFontSize: 22 },
  },
}

/* ── MAC 极简 · 浅 ── */
const MAC_LIGHT_THEME = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#007aff',
    colorInfo: '#007aff',
    colorSuccess: '#248a3d',
    colorError: '#d70015',
    colorWarning: '#c93400',
    colorLink: '#007aff',
    colorBgBase: '#f5f5f7',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: 'rgba(0,0,0,0.10)',
    colorBorderSecondary: 'rgba(0,0,0,0.07)',
    colorText: '#1d1d1f',
    colorTextSecondary: '#424245',
    colorTextTertiary: '#6e6e73',
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 6,
    fontSize: 12.5,
    controlHeight: 30,
    fontFamily: MAC_FONT_STACK,
  },
  components: {
    Card: { colorBgContainer: '#ffffff', headerBg: 'transparent' },
    Table: {
      headerBg: '#f5f5f7',
      headerColor: '#6e6e73',
      rowHoverBg: 'rgba(0,0,0,0.03)',
      borderColor: 'rgba(0,0,0,0.07)',
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
