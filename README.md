# SOL-USDT-SWAP · OKX 永续合约 K 线数据

本仓库保存 OKX `SOL-USDT-SWAP`（SOL USDT 本位永续合约）的已收盘 OHLCV K 线数据，并通过 GitHub Actions 每 2 小时增量更新一次。

> **时区标准：本仓库全部可读时间均使用北京时间（Asia/Shanghai，UTC+08:00）。**
> 数据仅用于研究、回测和分析，不构成交易建议或收益保证。

## 数据文件

每个 CSV 始终保留最近 **180 天的已收盘 K 线**，按 K 线开盘时间升序排列。由于周期不同，各文件行数不同：

| 周期 | 文件 | K 线数量 |
|---|---|---:|
| 5 分钟 | `SOL-USDT-SWAP_5m_180d_confirmed.csv` | 51,840 |
| 15 分钟 | `SOL-USDT-SWAP_15m_180d_confirmed.csv` | 17,280 |
| 30 分钟 | `SOL-USDT-SWAP_30m_180d_confirmed.csv` | 8,640 |
| 1 小时 | `SOL-USDT-SWAP_1H_180d_confirmed.csv` | 4,320 |
| 2 小时 | `SOL-USDT-SWAP_2H_180d_confirmed.csv` | 2,160 |

`metadata.json` 记录每个周期的数据覆盖范围、校验信息、更新模式、文件 SHA-256 和统一时区定义。

## 统一时间字段

CSV 文件字段如下：

```text
open_time_beijing, timestamp_ms,
open, high, low, close,
vol, volCcy, volCcyQuote, confirm
```

- `open_time_beijing`：K 线开盘时间，格式为 ISO 8601，固定使用北京时间，例如 `2026-09-18T23:15:00+08:00`。
- `timestamp_ms`：同一开盘时刻的 Unix Epoch 毫秒值。这是与时区无关的绝对时间键，**不加 8 小时**，用于排序、去重、连续性校验和跨系统对齐。
- `vol`、`volCcy`、`volCcyQuote`：保留 OKX K 线 API 的原始字段名称和数值。
- `confirm=1`：该 K 线已经收盘；未收盘的当前 K 线不会写入 CSV。

因此，所有供人阅读、展示和记录的日期时间均为东八区北京时间；机器时间键只保留标准 Unix Epoch 表示，避免同一根 K 线被错误偏移 8 小时。

## 增量更新脚本

脚本：[`download_okx_sol_perp_klines.py`](download_okx_sol_perp_klines.py)

```bash
python3 download_okx_sol_perp_klines.py
```

### 运行逻辑

- **首次运行、文件缺失、CSV 损坏或数据断档超出近期窗口时**：自动回退为完整历史回填，下载各周期最近 180 天的已收盘 K 线，并以北京时间格式写入。
- **正常后续运行**：每个周期只请求最近 300 根 K 线，和本地数据做重叠合并、去重、连续性校验，再裁剪为最近 180 天对应的 K 线数量。
- 为修正近期数据，最近 300 根已收盘 K 线会参与重叠覆盖，不只追加时间戳更大的行。
- 若没有新的或被修订的已收盘 K 线，CSV 与 `metadata.json` 不会被改写。
- 旧版同时包含 `open_time_utc` 与 `open_time_shanghai` 的 CSV，会在下一次成功运行时自动迁移为统一的北京时区字段。

常用命令：

```bash
# 仅检查 API、合并逻辑和完整性；不写入文件
python3 download_okx_sol_perp_klines.py --dry-run

# 无视本地数据，强制重新回填每个周期最近 180 天的 K 线
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

- GitHub Actions 的 cron 语法按 UTC 解释；`7 */2 * * *` 对应北京时间每天 `00:07、02:07、04:07 … 22:07`；
- 工作流和 Python 进程均设置 `TZ=Asia/Shanghai`；
- 第 7 分钟为新收盘 K 线留出短暂确认缓冲；
- 支持在 Actions 页面手动运行；手动运行时可选择 `full_refresh=true`；
- 使用 GitHub Actions 内置 `GITHUB_TOKEN` 和 `contents: write` 权限，仅在数据实际变化时提交 CSV 与 `metadata.json`；
- GitHub 的 schedule 属于尽力调度，可能延迟，因此不应用于对秒级时效有要求的实时交易系统。

## 免责声明

历史行情存在 API 延迟、修订、网络失败和交易所数据口径变化等风险。使用本仓库数据进行任何交易、研究或自动化决策前，应自行验证数据质量、费用、滑点、资金费和风险控制假设。
