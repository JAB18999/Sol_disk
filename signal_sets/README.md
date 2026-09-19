# 信号集（Signal Sets）

此目录专门保存交易信号集的 Markdown 文件。

建议每个信号集单独使用一个 `.md` 文件，并在文件中写明：

- 信号集名称与版本；
- 适用品种、周期与市场条件；
- 信号类别与触发条件；
- 确认条件、失效条件与优先级；
- 对应的开仓、加仓、减仓或反手动作；
- 风险限制、回测范围和修订记录。

示例文件名：`trend_breakout_v1.md`、`mean_reversion_v1.md`。

## 回测与优化文件

每个信号集的回测脚本、参数优化结果、回测报告、逐笔成交 CSV 和权益曲线 CSV 也保存在本目录，便于将信号定义、执行规则与可复现的研究结果放在一起。

当前 MACD 相关文件：

- `macd_backtest_optimizer.py`：MACD 信号集的事件驱动参数优化脚本；
- `macd_parameter_optimization_results.csv`：全参数组合的训练与样本外结果；
- `macd_backtest_report.md`：优化假设、信号映射、排名和风险提示；
- `macd_best_trades_<周期>.csv`：各周期最佳训练参数在样本外区间的成交记录；
- `macd_best_equity_<周期>.csv`：各周期最佳训练参数在样本外区间的权益曲线。
