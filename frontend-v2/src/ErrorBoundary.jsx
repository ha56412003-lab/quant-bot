import { Component } from 'react'

/**
 * ErrorBoundary —— 一块内容崩了，别让整个屏幕黑掉
 *
 * 为什么需要它（2026-11-11 的真实事故）
 * ------------------------------------
 * ConfigPage 里一行裸 fetch 拿到 401，401 的响应体 {detail:...} 被当成数据，
 * 接着 info.exchange.toUpperCase() 抛 TypeError，React 直接把整棵树卸载
 * → 整个页面白屏，连「总览」都点不回去。
 *
 * React 的错误边界就是干这个的：某个子树渲染抛异常，只卸载这一棵子树，
 * 换成我们自己的兜底 UI，外面照常活。
 *
 * 用法
 * ----
 *   <ErrorBoundary name="策略库"><StrategiesPage /></ErrorBoundary>
 *
 *   name     —— 出错时显示的名字（「策略库加载失败」），不传就写「这一块」
 *   compact  —— panel 级别的紧凑样式（不带大标题、字号小）
 *   onReset  —— 点「重试」时的额外回调（比如让父组件重新拉数据）
 *
 * 会做什么
 * --------
 * 1. 渲染兜底 UI：出错的名字 + 错误信息 + 「重试」按钮
 * 2. console.error 打出来（事后排查用）
 * 3. 往 window.__smokeErrs 里塞一条（★ scripts/smoke_pages.py 会自动抓到）
 *
 * 注意：错误边界抓不到的东西
 * --------------------------
 * - 事件处理器里抛的错（onClick 之类）→ 用 window.onerror 抓
 * - 异步代码（setTimeout / Promise）里抛的错 → 用 unhandledrejection 抓
 * - 服务端渲染
 * 这几类由 smoke_pages.py 注入的钩子兜住，两边配合才完整。
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { err: null, info: null }
    this.handleReset = this.handleReset.bind(this)
  }

  static getDerivedStateFromError(err) {
    return { err }
  }

  componentDidCatch(err, info) {
    const name = this.props.name || 'unknown'

    // 1) 控制台留痕（DevTools 里能直接看到堆栈）
    console.error('[ErrorBoundary] ' + name + ' 崩了', err, info)

    // 2) 报给冒烟脚本：scripts/smoke_pages.py 会读这个数组
    //    这样「页面看起来没白屏，但某块其实是兜底 UI」也能被测出来
    try {
      window.__smokeErrs = window.__smokeErrs || []
      window.__smokeErrs.push(
        '[boundary:' + name + '] ' + String((err && (err.message || err)) || '')
      )
    } catch (e) {
      /* 连记日志都失败就算了，绝不能因此再崩一次 */
    }

    this.setState({ info })
  }

  handleReset() {
    this.setState({ err: null, info: null })
    if (typeof this.props.onReset === 'function') {
      try {
        this.props.onReset()
      } catch (e) {
        console.error('[ErrorBoundary] onReset 失败', e)
      }
    }
  }

  render() {
    const { err, info } = this.state
    if (!err) return this.props.children

    const name = this.props.name || '这一块'
    const msg = String((err && (err.message || err)) || '未知错误')
    const compact = !!this.props.compact

    const box = {
      border: '1px solid var(--line, #262a39)',
      borderLeft: '3px solid #c4182c',
      borderRadius: 3,
      background: 'rgba(196,24,44,0.06)',
      padding: compact ? '10px 12px' : '20px 22px',
      margin: compact ? 0 : '12px 0',
      color: 'var(--fore, #eef0f7)',
      fontSize: compact ? 11.5 : 13,
      lineHeight: 1.6,
    }

    return (
      <div style={box}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: compact ? 4 : 10 }}>
          <span style={{ color: '#c4182c', fontWeight: 700 }}>
            {compact ? '⚠ ' + name + ' 加载失败' : '⚠ ' + name + ' 加载失败'}
          </span>
          <button
            onClick={this.handleReset}
            style={{
              marginLeft: 'auto',
              padding: '3px 12px',
              fontSize: compact ? 11 : 12,
              cursor: 'pointer',
              background: 'transparent',
              color: 'var(--joint, #ff7fcb)',
              border: '1px solid var(--joint, #ec4fae)',
              borderRadius: 3,
            }}
          >
            重试
          </button>
        </div>

        <div style={{ color: 'var(--mute, #73788e)', fontSize: compact ? 11 : 12, wordBreak: 'break-word' }}>
          {msg}
        </div>

        {compact ? null : (
          <details style={{ marginTop: 10 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--mute, #73788e)', fontSize: 11 }}>
              展开堆栈（贴给罗小黑看）
            </summary>
            <pre
              style={{
                marginTop: 8,
                padding: 10,
                background: 'rgba(0,0,0,0.25)',
                borderRadius: 3,
                fontSize: 10.5,
                lineHeight: 1.5,
                overflow: 'auto',
                maxHeight: 240,
                color: 'var(--mute, #9aa0b4)',
              }}
            >
              {msg}
              {'\n\n'}
              {String((info && info.componentStack) || '')}
            </pre>
          </details>
        )}

        <div
          style={{
            marginTop: compact ? 6 : 12,
            fontSize: compact ? 10.5 : 11,
            color: 'var(--mute, #73788e)',
            opacity: 0.75,
          }}
        >
          这只是页面上的一块内容加载失败 —— 策略引擎、持仓和止损都在后端，照常跑。
        </div>
      </div>
    )
  }
}
