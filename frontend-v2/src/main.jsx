import './v2.css'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import App from './App'
import { I18nProvider, useI18n } from './i18n'

function LocaleShell() {
  const { lang } = useI18n()
  return (
    <ConfigProvider
      locale={lang === 'en' ? enUS : zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          // ——— SPIDER 战术大屏配色（洋红框 + 青色数据 + 近黑底，调暗一档更耐看）
          colorPrimary: '#c1207e',          // 洋红（品牌/边框/强调）
          colorInfo: '#1fa8c4',             // 青色（数据/连接）
          colorSuccess: '#3fa86a',
          colorError: '#d4595e',
          colorWarning: '#d0a03a',
          colorLink: '#1fa8c4',
          colorBgBase: '#05070a',           // 近黑底
          colorBgContainer: '#0e131a',      // 面板
          colorBgElevated: '#141b24',       // 弹层/浮层
          colorBorder: 'rgba(193,32,126,0.30)',
          colorBorderSecondary: 'rgba(193,32,126,0.15)',
          colorText: '#f2f6fa',
          colorTextSecondary: '#8a96a8',
          colorTextTertiary: '#6b7787',
          borderRadius: 3,                  // 小圆角（战术屏特征）
          borderRadiusLG: 4,
          fontSize: 12.5,
          controlHeight: 30,
          fontFamily: "'Cascadia Mono', 'JetBrains Mono', Consolas, 'Microsoft YaHei', 'PingFang SC', monospace",
        },
        components: {
          Card: {
            colorBgContainer: 'rgba(14,19,26,0.92)',
            headerBg: 'rgba(20,27,36,0.75)',
          },
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
      }}
    >
      <App />
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <I18nProvider>
    <LocaleShell />
  </I18nProvider>
)
