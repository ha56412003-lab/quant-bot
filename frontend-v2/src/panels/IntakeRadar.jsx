import { useRef, useEffect } from 'react'

/* ══════════════════════════════════════════════════════════
   进料雷达 INTAKE RADAR — 从参考站 index.html canvas 源码复刻
   原坐标：底座(1116,516)-(1190,524) 柱体(1110,524,80,28)
          立杆(1150,516→494) 碟面 translate(1150,482) rotate(-.55) ellipse 28×19
          铭牌(1116,532,68,12)
   裁剪范围 x 1092..1214  y 418..558
   注意：绿色信号弧圆心在 (1160,466)、半径最大 42，必须留出上方空间，
        否则弧会画到画布外（曾因此完全看不见动效）
   ══════════════════════════════════════════════════════════ */

const OX = 1092, OY = 418
const W = 124, H = 142

const MONO = '"JetBrains Mono","Noto Sans SC","PingFang SC",ui-monospace,Menlo,monospace'
const TAU = Math.PI * 2

export default function IntakeRadar({ width = 104 }) {
  const ref = useRef(null)

  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const dpr = 2
    cv.width = W * dpr
    cv.height = H * dpr
    const c = cv.getContext('2d')

    let raf = 0
    const draw = (t) => {
      c.setTransform(dpr, 0, 0, dpr, 0, 0)
      c.clearRect(0, 0, W, H)
      c.save()
      c.translate(-OX, -OY)

      /* ───── 底座梯形 ───── */
      c.fillStyle = '#20232e'
      c.beginPath()
      c.moveTo(1116, 516); c.lineTo(1184, 516); c.lineTo(1190, 524); c.lineTo(1110, 524)
      c.closePath(); c.fill()

      /* ───── 底座箱体 ───── */
      c.fillStyle = '#15171f'; c.fillRect(1110, 524, 80, 28)
      c.strokeStyle = '#2a2e3b'; c.strokeRect(1110.5, 524.5, 79, 27)

      /* ───── 立杆 ───── */
      c.strokeStyle = '#6b7184'; c.lineWidth = 3
      c.beginPath(); c.moveTo(1150, 516); c.lineTo(1150, 494); c.stroke()

      /* ───── 碟面（旋转 -0.55 rad）───── */
      c.save()
      c.translate(1150, 482)
      c.rotate(-0.55)
      c.fillStyle = '#cfd3dc'
      c.beginPath(); c.ellipse(0, 0, 28, 19, 0, 0, TAU); c.fill()
      // 同心环（3 层）
      c.strokeStyle = '#8b91a3'; c.lineWidth = 1
      for (let k = 1; k < 4; k++) {
        c.beginPath(); c.ellipse(0, 0, 28 * k / 4, 19 * k / 4, 0, 0, TAU); c.stroke()
      }
      // 十字准线
      c.beginPath()
      c.moveTo(-28, 0); c.lineTo(28, 0)
      c.moveTo(0, -19); c.lineTo(0, 19)
      c.stroke()
      // 馈源臂
      c.strokeStyle = '#4a5064'; c.lineWidth = 2
      c.beginPath(); c.moveTo(0, 0); c.lineTo(10, -24); c.stroke()
      c.fillStyle = '#2a2e3b'
      c.beginPath(); c.arc(10, -24, 3, 0, TAU); c.fill()
      c.restore()

      /* ───── 信号弧（3 条绿色波纹，从碟面朝右上方扩散）─────
         圆心放在碟面中心 (1150,482)，上方留够空间才能看见 */
      const ph = (t / 1000) % 2.4
      for (let k = 0; k < 3; k++) {
        const p = (ph + k * 0.8) % 2.4
        const a = Math.max(0, 1 - p / 1.6)
        if (a <= 0.01) continue
        c.strokeStyle = `rgba(76, 240, 176, ${a})`
        c.lineWidth = 2.4
        c.lineCap = 'round'
        c.beginPath()
        c.arc(1150, 482, 20 + p * 15, -1.15, -0.35)
        c.stroke()
      }

      /* ───── 铭牌 ───── */
      c.fillStyle = '#0c0d12'; c.fillRect(1116, 532, 68, 12)
      c.strokeStyle = '#3a3f50'; c.strokeRect(1116.5, 532.5, 67, 11)
      c.fillStyle = '#e9ebf3'
      c.font = `800 7px ${MONO}`
      c.textAlign = 'center'
      c.fillText('INTAKE RADAR', 1150, 541)
      c.textAlign = 'left'

      c.restore()
      raf = requestAnimationFrame(draw)
    }
    draw(0)                 // 先同步画一帧，rAF 可能被节流
    return () => cancelAnimationFrame(raf)
  }, [])

  return (
    <canvas
      ref={ref}
      style={{ width: width, height: Math.round(width * H / W), display: 'block' }}
    />
  )
}
