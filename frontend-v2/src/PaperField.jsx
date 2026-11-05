import React from 'react'

/**
 * 纸面动态背景层（PaperField）
 * ------------------------------------------------------------
 * 对应源站「#ops 纸面主题」里那层流动线条的氛围：
 *   · 极淡的同心弧，缓慢反向旋转
 *   · 从左侧圆心向外发散的谱线，长度按正弦波呼吸
 *   · 缓慢漂移的细小微粒
 *   · 一道很淡的扫描光带自上而下巡游
 *
 * 全部自己实现的简单数学，不引用任何第三方绘制代码。
 * 只在 data-theme="paper" 时渲染，切回玫红主题即卸载。
 *
 * 性能约束（很重要）：
 *   · 30fps 上限，不跑满 60
 *   · 粒子固定 30 个
 *   · 页面切到后台（visibilitychange）自动暂停
 *   · 整个 canvas 设 pointer-events:none，不挡任何交互
 */
export default function PaperField() {
  const ref = React.useRef(null)
  const [modeKey, setModeKey] = React.useState(0)

  // 监听 data-theme 变化，切换时重新挂载/卸载
  React.useEffect(() => {
    const root = document.documentElement
    const obs = new MutationObserver(() => {
      setModeKey((k) => k + 1)
    })
    obs.observe(root, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])

  React.useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return

    let raf = 0
    let running = true
    let w = 0
    let h = 0
    let dpr = Math.min(window.devicePixelRatio || 1, 2)

    // ── 尺寸 ──
    const resize = () => {
      w = window.innerWidth
      h = window.innerHeight
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.floor(w * dpr)
      canvas.height = Math.floor(h * dpr)
      canvas.style.width = w + 'px'
      canvas.style.height = h + 'px'
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()

    // ── 粒子（固定数量，位置随机） ──
    const P_N = 30
    const parts = Array.from({ length: P_N }, () => ({
      x: Math.random(),
      y: Math.random(),
      vx: (Math.random() - 0.5) * 0.00013,
      vy: (Math.random() - 0.5) * 0.00010,
      r: 0.9 + Math.random() * 1.7,
      a: 0.13 + Math.random() * 0.20,
    }))

    let t = 0

    const INK = '150,185,210'  // 冷白微光：深色卡片上发亮，纸白底上微微发灰 // 墨色

    /** 一侧的竖向弧带：贴边、不侵入中间 */
    const drawSideArcs = (side) => {
      const cx = side < 0 ? w * 0.015 : w * 0.985
      const cy = h * 0.50
      const maxR = Math.min(w * 0.22, h * 0.85)

      for (let i = 0; i < 4; i++) {
        const r = maxR * (0.30 + i * 0.22)
        const dir = (i % 2 === 0 ? 1 : -1) * (side < 0 ? 1 : -1)
        const sweep = Math.PI * (0.55 + i * 0.12)
        const start = -sweep / 2 + t * 0.00020 * dir

        ctx.beginPath()
        ctx.arc(cx, cy, r, start, start + sweep)
        ctx.lineWidth = 1
        ctx.strokeStyle = `rgba(${INK},${(0.22 - i * 0.040).toFixed(3)})`
        ctx.stroke()
      }
    }

    /** 从边缘向内射的短谱线（呼吸） */
    const drawSideSpokes = (side) => {
      const x0 = side < 0 ? 0 : w
      const N = 22
      for (let i = 0; i < N; i++) {
        const y = (i / (N - 1)) * h
        const ph = Math.sin(t * 0.0007 + i * 0.55)
        const len = w * (0.018 + 0.030 * (0.5 + 0.5 * ph))
        const alpha = 0.11 + 0.12 * (0.5 + 0.5 * ph)

        ctx.beginPath()
        ctx.moveTo(x0, y)
        ctx.lineTo(x0 + (side < 0 ? len : -len), y)
        ctx.lineWidth = 1
        ctx.strokeStyle = `rgba(${INK},${alpha.toFixed(3)})`
        ctx.stroke()
      }
    }

    /** 顶部水平流线：2 条极淡长线，缓慢横向漂 */
    const drawTopLines = () => {
      for (let i = 0; i < 2; i++) {
        const y = h * (0.10 + i * 0.055)
        const off = (t * 0.00002 * (i + 1)) % 1
        const x1 = -w * 0.2 + off * w
        const x2 = x1 + w * (0.55 + i * 0.2)
        const g = ctx.createLinearGradient(x1, 0, x2, 0)
        g.addColorStop(0, `rgba(${INK},0)`)
        g.addColorStop(0.5, `rgba(${INK},${0.12 - i * 0.04})`)
        g.addColorStop(1, `rgba(${INK},0)`)
        ctx.beginPath()
        ctx.moveTo(x1, y)
        ctx.lineTo(x2, y)
        ctx.lineWidth = 1
        ctx.strokeStyle = g
        ctx.stroke()
      }
    }

    const draw = () => {
      if (!running) return
      ctx.clearRect(0, 0, w, h)

      drawTopLines()
      drawSideArcs(-1)
      drawSideArcs(1)
      drawSideSpokes(-1)
      drawSideSpokes(1)

      // 粒子漂移
      for (const p of parts) {
        p.x += p.vx; p.y += p.vy
        if (p.x < -0.02) p.x = 1.02
        if (p.x > 1.02) p.x = -0.02
        if (p.y < -0.02) p.y = 1.02
        if (p.y > 1.02) p.y = -0.02

        ctx.beginPath()
        ctx.arc(p.x * w, p.y * h, p.r, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(${INK},${p.a.toFixed(3)})`
        ctx.fill()
      }

      // 扫描光带：很淡
      const bandY = ((t * 0.00005) % 1) * (h + 200) - 100
      const grad = ctx.createLinearGradient(0, bandY - 80, 0, bandY + 80)
      grad.addColorStop(0, `rgba(${INK},0)`)
      grad.addColorStop(0.5, `rgba(${INK},0.070)`)
      grad.addColorStop(1, `rgba(${INK},0)`)
      ctx.fillStyle = grad
      ctx.fillRect(0, bandY - 80, w, 160)

      t += 16.7
    }

    // ── 30fps 节流 ──
    let last = 0
    const FRAME = 1000 / 30
    const loop = (ts) => {
      raf = requestAnimationFrame(loop)
      if (ts - last < FRAME) return
      last = ts
      draw()
    }
    raf = requestAnimationFrame(loop)

    // ── 后台自动暂停，回来恢复 ──
    const onVis = () => {
      if (document.hidden) {
        running = false
        cancelAnimationFrame(raf)
      } else {
        running = true
        last = 0
        raf = requestAnimationFrame(loop)
      }
    }
    const onResize = () => { resize() }

    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('resize', onResize)

    return () => {
      running = false
      cancelAnimationFrame(raf)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('resize', onResize)
    }
  }, [modeKey])

  // 只有纸面模式才画
  if (typeof document === 'undefined') return null
  if (document.documentElement.getAttribute('data-theme') !== 'paper') return null

  return (
    <canvas
      ref={ref}
      aria-hidden="true"
      style={{
        position: 'fixed',
        inset: 0,
        pointerEvents: 'none',
        zIndex: 9996,       // 高于内容，否则深色卡片会盖住边缘弧线
        opacity: 1,
      }}
    />
  )
}
