# 交易日记(机器可读,供 AI 策略优化)

每行一个 JSON(JSONL),按 UTC 日期分文件:`trades-YYYY-MM-DD.jsonl`。

| 字段 | 含义 |
|---|---|
| closed_at / opened_at | 平仓/开仓时间(UTC ISO) |
| strategy / symbol / side | 策略ID / 交易对 / long·short |
| qty / entry_price / close_price | 数量 / 开仓价 / 平仓价 |
| pnl / pnl_pct | 已实现盈亏(USDT,含手续费)/ 相对开仓名义的百分比 |
| hold_minutes | 持仓时长(分钟) |
| signal_reason | 开仓时的信号描述(入场依据) |
| close_reason | 平仓原因(止损/止盈/移动止损/时限/策略出场/手动) |
| mfe_pct / mae_pct | 持仓期间最大有利偏移 / 最大不利偏移(%,0.5s 采样)——校准止盈止损的核心数据 |
| entry_factors / exit_factors | 开仓/平仓时刻的关键因子快照(费率、多空比、OI变化、taker、爆仓倍数、基差、恐贪等) |
| mode | paper / live |

AI 优化建议用法:按 strategy 分组,对比盈利/亏损样本的 entry_factors 分布差异;
用 MFE/MAE 分布评估当前止盈止损倍数是否留利润/砍太早。
