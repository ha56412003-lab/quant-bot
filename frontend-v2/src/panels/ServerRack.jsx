
/**
 * 服务器机柜（像素风）
 * 完全照搬 F:\agent-display 里 canvas 的绘制坐标：
 *   顶部斜面 polygon (1244,292)-(1314,292)-(1318,298)-(1240,298)
 *   柜体 fillRect(1240, 298, 78, 252)
 *   14 个机位 fillRect(1246, 306 + k*17, 66, 13)
 *   标签 GPU ×8
 * 坐标整体平移到 (0,0) 起点，viewBox 78×258。
 */
export default function ServerRack({ width = 26, className = '' }) {
  const rows = Array.from({ length: 14 })
  return (
    <svg
      className={'srv-rack ' + className}
      width={width}
      height={width * 258 / 78}
      viewBox="0 0 78 258"
      aria-hidden="true"
    >
      {/* 顶部散热斜面 */}
      <polygon points="4,0 74,0 78,6 0,6" fill="#20232e" />
      {/* 柜体 */}
      <rect x="0" y="6" width="78" height="252" fill="#111319" stroke="#262a36" strokeWidth="1" />
      {/* 14 个机位 + 右侧指示灯 */}
      {rows.map((_, k) => (
        <g key={k}>
          <rect x="6" y={14 + k * 17} width="66" height="13" fill="#171a22" />
          {/* 面板散热条 */}
          <rect x="10" y={18 + k * 17} width="34" height="2" fill="#232733" />
          {/* 绿灯：逐格交错闪烁 */}
          <rect
            className="rack-led"
            x="62"
            y={18 + k * 17}
            width="6"
            height="4"
            style={{ animationDelay: `${(k % 5) * 0.18}s` }}
          />
          {/* 红灯：隔一格一个，闪得比绿灯急 */}
          {k % 2 === 0 && (
            <rect
              className="rack-led-red"
              x="54"
              y={18 + k * 17}
              width="6"
              height="4"
              style={{ animationDelay: `${(k % 4) * 0.22}s` }}
            />
          )}
        </g>
      ))}
      {/* 标签 */}
      {/* 顶部报警灯 */}
      <circle className="rack-alarm" cx="70" cy="4.2" r="2.3" />
      {/* 标签 */}
      <text x="10" y="253" fill="#8b90a6" fontSize="7" fontWeight="700"
            fontFamily="Cascadia Mono, JetBrains Mono, Consolas, monospace">
        GPU ×8
      </text>
    </svg>
  )
}
