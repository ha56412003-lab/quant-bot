// 币种专属糖果色 —— 从 agent-display #spider 的 agent 调色板提取（index.html:4187-4215）
// 12 个饱和糖果色，去掉偏白 #9aa6ff（深底上与白字分不清）
export const COIN_COLORS = [
  '#2fd27a', '#a774ff', '#ff4458', '#ff5cae', '#3b7cff', '#25d6f0',
  '#ff9a1f', '#ffd23f', '#ff7b54', '#b5ef3a', '#62e6c7', '#45e6f5',
]

/**
 * 稳定哈希：同一个 sym 永远返回同一个色，不同 sym 大概率不同色。
 * 用法：coinColor('BTCUSDT') → '#a774ff'（固定）
 *      coinColor('SOLUSDT') → '#3b7cff'（固定）
 */
export function coinColor(sym) {
  if (!sym) return COIN_COLORS[0]
  let h = 0
  for (let i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) >>> 0
  return COIN_COLORS[h % COIN_COLORS.length]
}
