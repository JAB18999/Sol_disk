# SOL USDT 永续合约 K 线数据

本仓库保存从 OKX Public API 下载的 `SOL-USDT-SWAP`（SOL USDT 本位永续合约）历史 K 线快照。

## 本次数据快照

- 数据源：OKX Public API `GET /api/v5/market/history-candles`
- 下载时间（UTC）：2026-09-16T15:35:58.950642Z
- 筛选条件：仅保留 `confirm=1` 的已收盘 K 线
- 排序：按 K 线开盘时间升序
- 每个周期：8,640 根连续、唯一的 K 线，已完成时间间隔完整性检查

| 周期 | CSV 文件 | 覆盖区间（UTC，K 线开盘时间） |
|---|---|---|
| 15m | `SOL-USDT-SWAP_15m_8640_confirmed.csv` | 2026-06-18 15:30 → 2026-09-16 15:15 |
| 30m | `SOL-USDT-SWAP_30m_8640_confirmed.csv` | 2026-03-20 15:30 → 2026-09-16 15:00 |
| 1H | `SOL-USDT-SWAP_1H_8640_confirmed.csv` | 2025-09-21 15:00 → 2026-09-16 14:00 |
| 2H | `SOL-USDT-SWAP_2H_8640_confirmed.csv` | 2024-09-26 14:00 → 2026-09-16 12:00 |

## 字段说明

每个 CSV 包含以下字段：

```text
open_time_utc, open_time_shanghai, timestamp_ms,
open, high, low, close,
vol, volCcy, volCcyQuote, confirm
```

- `timestamp_ms` 和时间字段表示 K 线的开盘时刻。
- `vol`、`volCcy`、`volCcyQuote` 保留 OKX K 线接口的原始字段名称和数值。
- `confirm=1` 表示该 K 线已经收盘。

## 可复现下载

`download_okx_sol_perp_klines.py` 会从 OKX 下载上述四个周期各最新的 8,640 根已收盘 K 线，并生成数据范围、SHA-256 与完整性检查记录到 `metadata.json`。

```bash
python3 download_okx_sol_perp_klines.py
```

> 本仓库刻意不提交 Excel 工作簿和 ZIP 压缩包，以避免和原始 CSV 重复版本化。
