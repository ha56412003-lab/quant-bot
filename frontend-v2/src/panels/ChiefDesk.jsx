import { useRef, useEffect } from 'react'

/* ══════════════════════════════════════════════════════════
   总调度台 CHIEF — 从参考站 index.html canvas 源码逐像素复刻
   原坐标：屏幕(1104,196) 桌面(1092..1308,226..240) 前板(1084,240,224,24)
   CHIEF 位置 (1196, 204) r=13  col=#f3f4f8  shape='robot'
   裁剪范围 x 1078..1316  y 172..292
   ══════════════════════════════════════════════════════════ */

const OX = 1078, OY = 172           // 原点偏移
const W = 238, H = 120              // 画布逻辑尺寸

const MONO = '"JetBrains Mono","Noto Sans SC","PingFang SC",ui-monospace,Menlo,monospace'
const TAU = Math.PI * 2

const C = { screen: '#05070c', desk: '#2a2e3b', deskFront: '#181b25', joint: '#ff3f8e', sticky: '#ffd84a' }
const CHIEF = { k: 'CHIEF', col: '#f3f4f8', shape: 'robot' }

function rr(c, x, y, w, h, r) {
  c.beginPath()
  c.moveTo(x + r, y)
  c.arcTo(x + w, y, x + w, y + h, r)
  c.arcTo(x + w, y + h, x, y + h, r)
  c.arcTo(x, y + h, x, y, r)
  c.arcTo(x, y, x + w, y, r)
  c.closePath()
}

/* 身体 + 脸（robot 分支：耳机带 + 两侧耳罩 + 粉色耳罩灯 + 黑眼） */
function drawBody(c, a, x, y, r, opt) {
  opt = opt || {}
  c.save()
  c.translate(x, y)
  // base shape：robot 走 default 圆形
  c.beginPath()
  c.arc(0, 0, r, 0, TAU)
  c.fillStyle = a.col
  c.fill()
  c.lineWidth = 1
  c.strokeStyle = 'rgba(0,0,0,.35)'
  c.stroke()

  if (a.shape === 'robot') {
    // 耳机头带
    c.strokeStyle = '#1a1c25'
    c.lineWidth = 3
    c.beginPath()
    c.arc(0, -1, r + 2.5, Math.PI * 1.08, Math.PI * 1.92)
    c.stroke()
    // 左右耳罩
    c.fillStyle = '#1a1c25'
    rr(c, -r - 5, -5, 6, 11, 2); c.fill()
    rr(c, r - 1, -5, 6, 11, 2); c.fill()
    // 耳罩灯（洋红 #ff3f8e）
    c.fillStyle = C.joint
    c.beginPath(); c.arc(-r - 2, 0, 1.6, 0, TAU); c.fill()
    c.beginPath(); c.arc(r + 2, 0, 1.6, 0, TAU); c.fill()
  }

  // 眼睛：col 亮度 > .8 走深色眼（CHIEF 白脸 → 黑眼）
  const light = true   // #f3f4f8 亮度 ≈ .95
  const ex = r * 0.38, ey = -r * 0.12, er = r * 0.3
  const bl = opt.blink ? 0.15 : 1
  const lx = (opt.look || 0) * er * 0.45
  if (light) {
    c.fillStyle = '#111318'
    c.beginPath(); c.ellipse(-ex + lx * 0.6, ey, er * 0.55, er * 0.95 * bl, 0, 0, TAU); c.fill()
    c.beginPath(); c.ellipse(ex + lx * 0.6, ey, er * 0.55, er * 0.95 * bl, 0, 0, TAU); c.fill()
  }
  c.restore()
}

/* 桌上蜘蛛徽标 */
function drawMark(c, x, y, s, col) {
  const k = s / 32
  c.save()
  c.translate(x - 16 * k, y - 16 * k)
  c.scale(k, k)
  c.strokeStyle = col
  c.lineWidth = 2
  c.lineCap = 'round'
  c.lineJoin = 'round'
  const L = [[13, 13, 8, 7, 5, 2], [12, 15, 5, 12, 1, 13], [12, 17, 5, 19, 1, 23], [13, 19, 9, 25, 6, 30]]
  c.beginPath()
  for (const [a, b, p, q, u, v] of L) {
    c.moveTo(a, b); c.lineTo(p, q); c.lineTo(u, v)
    c.moveTo(32 - a, b); c.lineTo(32 - p, q); c.lineTo(32 - u, v)
  }
  c.stroke()
  c.fillStyle = col
  c.beginPath()
  c.ellipse(16, 16, 4.6, 6, 0, 0, TAU)
  c.fill()
  c.restore()
}

export default function ChiefDesk({ width = 238, lookMode = 'idle' }) {
  const ref = useRef(null)

  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    // 像素密度跟随显示宽度：空仓放大到 430px 时 dpr 升到 4，避免位图糊
    const dpr = Math.max(2, Math.ceil((width / W) * 2))
    cv.width = W * dpr
    cv.height = H * dpr
    const c = cv.getContext('2d')
    let raf = 0
    let blinkT = 2 + Math.random() * 3
    const t0 = performance.now()

    const frame = (now) => {
      const t = (now - t0) / 1000
      c.setTransform(dpr, 0, 0, dpr, 0, 0)
      c.clearRect(0, 0, W, H)
      c.save()
      c.translate(-OX, -OY)

      // 眨眼（每 2~5 秒一次）
      blinkT -= 1 / 60
      let blink = false
      if (blinkT < 0) { blink = true; if (blinkT < -0.12) blinkT = 2 + Math.random() * 3 }
      // 呼吸浮动
      const bob = Math.sin(t * 2) * 0.8
      // 视线：idle 缓慢左右扫，focus 时居中
      const look = lookMode === 'idle' ? Math.sin(t * 0.7) * 0.8 : 0

      /* ───── CHIEF 本人 ───── */
      const cx = 1196, cy = 204 + bob, cr = 13
      c.fillStyle = 'rgba(0,0,0,.45)'
      c.beginPath(); c.ellipse(cx, 204 + cr + 5, cr * 0.95, 3, 0, 0, TAU); c.fill()
      c.fillStyle = CHIEF.col
      c.beginPath(); c.arc(cx - cr - 4, cy + 3, 2.6, 0, TAU); c.fill()
      c.beginPath(); c.arc(cx + cr + 4, cy + 3, 2.6, 0, TAU); c.fill()
      drawBody(c, CHIEF, cx, cy, cr, { look, blink })
      c.fillStyle = CHIEF.col
      c.font = `800 8px ${MONO}`
      c.textAlign = 'center'
      c.fillText(CHIEF.k, cx, 204 - cr - 6)
      c.textAlign = 'left'

      /* ───── 屏幕（显示器）+ 扫描线 ───── */
      rr(c, 1104, 196, 46, 28, 2)
      c.fillStyle = '#1b1f2b'; c.fill()
      c.strokeStyle = '#2c3142'; c.stroke()
      c.fillStyle = C.screen; c.fillRect(1107, 199, 40, 22)
      const scan = 199 + ((t * 14) % 22)
      c.fillStyle = 'rgba(69,230,245,.22)'; c.fillRect(1107, scan, 40, 1.5)

      /* ───── 桌面（梯形）+ 前板 ───── */
      c.beginPath()
      c.moveTo(1092, 226); c.lineTo(1300, 226); c.lineTo(1308, 240); c.lineTo(1084, 240)
      c.closePath()
      c.fillStyle = C.desk; c.fill()
      c.fillStyle = C.deskFront; c.fillRect(1084, 240, 224, 24)
      c.strokeStyle = '#272b3a'; c.strokeRect(1084.5, 240.5, 223, 23)

      /* ───── 蜘蛛徽标 ───── */
      drawMark(c, 1196, 252, 16, '#d9dce6')

      /* ───── 杯子 + 便签 ───── */
      c.fillStyle = '#2f3443'; rr(c, 1238, 216, 20, 9, 2); c.fill()
      c.fillStyle = '#4a5064'; c.fillRect(1241, 213, 14, 3)
      c.fillStyle = '#d9dce6'; c.fillRect(1268, 214, 22, 11)
      c.fillStyle = '#9ca2b3'
      for (let k = 0; k < 3; k++) c.fillRect(1271, 216 + k * 3, 16, 1)

      /* ───── 黄色铭牌 ───── */
      c.fillStyle = C.sticky
      c.font = `700 7.5px ${MONO}`
      c.textAlign = 'center'
      c.fillText('HEAD OF THE FLOOR · 总调度台', 1196, 276)
      c.textAlign = 'left'

      c.restore()
      raf = requestAnimationFrame(frame)
    }
    frame(performance.now())
    return () => cancelAnimationFrame(raf)
  }, [lookMode])

  return (
    <canvas
      ref={ref}
      style={{ width: width, height: Math.round(width * H / W), display: 'block' }}
    />
  )
}
