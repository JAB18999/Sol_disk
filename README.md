# SOL-USDT-SWAP · OKX 永续合约 K 线数据

本仓库保存 OKX `SOL-USDT-SWAP`（SOL USDT 本位永续合约）的已收盘 OHLCV K 线数据，并通过 GitHub Actions 每 2 小时增量更新一次。

> 数据源：OKX Public API。K 线仅用于研究、回测和数据分析，不构成交易建议或收益保证。

## 数据文件

每个 CSV 始终保留最新的 **8,640 根已收盘 K 线**，按 K 线开盘时间升序排列：

| 周期 | 文件 | 近似覆盖长度 |
|---|---|---:|
| 15 分钟 | `SOL-USDT-SWAP_15m_8640_confirmed.csv` | 90 天 |
| 30 分钟 | `SOL-USDT-SWAP_30m_8640_confirmed.csv` | 180 天 |
| 1 小时 | `SOL-USDT-SWAP_1H_8640_confirmed.csv` | 360 天 |
| 2 小时 | `SOL-USDT-SWAP_2H_8640_confirmed.csv` | 720 天 |

`metadata.json` 记录每个周期最新的数据覆盖范围、校验信息、更新模式和文件 SHA-256。

## 字段说明

```text
open_time_utc, open_time_shanghai, timestamp_ms,
open, high, low, close,
vol, volCcy, volCcyQuote, confirm
```

- 时间戳表示 K 线的**开盘时刻**。
- `open_time_utc` 为 UTC 时间，`open_time_shanghai` 为 Asia/Shanghai 时间。
- `vol`、`volCcy`、`volCcyQuote` 保留 OKX K 线 API 的原始字段名称和数值。
- 仓库仅保留 `confirm=1` 的已收盘 K 线；未收盘的当前 K 线不会写入 CSV。

## 增量更新脚本

脚本：[`download_okx_sol_perp_klines.py`](download_okx_sol_perp_klines.py)

```bash
python3 download_okx_sol_perp_klines.py
```

### 运行逻辑

- **首次运行、文件缺失、CSV 损坏或数据断档超出近期窗口时**：自动回退为完整历史回填，下载各周期最新 8,640 根已收盘 K 线。
- **正常后续运行**：每个周期只请求最近 300 根 K 线，和本地数据做重叠合并、去重、连续性校验，再裁剪为最新 8,640 根。
- 为修正近期数据，最近 300 根已收盘 K 线会参与重叠覆盖，不只追加时间戳更大的行。
- 若没有新的或被修订的已收盘 K 线，CSV 与 `metadata.json` 不会被改写。

常用命令：

```bash
# 仅检查 API、合并逻辑和完整性；不写入文件
python3 download_okx_sol_perp_klines.py --dry-run

# 无视本地数据，强制重新回填每个周期的 8,640 根 K 线
python3 download_okx_sol_perp_klines.py --full-refresh
```

脚本仅依赖 Python 标准库。

## 请求节流与稳定性

更新器采用合规的限流保护，不使用代理轮换、IP 轮换或伪造多身份：

- 单进程全局节流：最少间隔 0.25 秒，即最多约 4 次请求/秒；
- 429 限流响应会执行带随机抖动的指数退避，并优先尊重 `Retry-After`；
- 400、401、403、404 等参数/权限问题会立即停止，不会重复撞击接口；
- 5xx、超时和网络错误最多重试 6 次；
- `.okx_candle_update.lock` 防止本地重复任务并发执行；
- CSV 与 metadata 使用临时文件加原子替换，避免中途失败写坏旧数据；
- GitHub Actions 使用并发组，避免计划任务与手动任务重叠。

## GitHub Actions：每 2 小时更新

工作流文件：[`.github/workflows/update-okx-sol-candles.yml`](.github/workflows/update-okx-sol-candles.yml)

- 计划：`7 */2 * * *`，即 **UTC 每个偶数小时的第 7 分钟**运行；
- 第 7 分钟为新收盘 K 线留出短暂确认缓冲；
- 支持在 Actions 页面手动运行；手动运行时可选择 `full_refresh=true`；
- 使用 GitHub Actions 内置 `GITHUB_TOKEN` 和 `contents: write` 权限，仅在数据实际变化时提交 CSV 与 `metadata.json`；
- GitHub 的 schedule 属于尽力调度，可能延迟，因此不应用于对秒级时效有要求的实时交易系统。

## 免责声明

历史行情存在 API 延迟、修订、网络失败和交易所数据口径变化等风险。使用本仓库数据进行任何交易、研究或自动化决策前，应自行验证数据质量、费用、滑点、资金费和风险控制假设。
