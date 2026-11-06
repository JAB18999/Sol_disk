# SOL_disk

OKX SOL-USDT-SWAP 永续合约市场数据仓库。全部数据来自 OKX 公开 API（无需 API Key），通过 GitHub Actions 每 6 小时增量更新一次，也可手动触发。

## 数据文件（`DATA/`）

K 线（已收盘 OHLCV，北京时间 UTC+8，每个粒度保留最新 360 天）：

| 文件 | 粒度 | 期望根数（360 天） | 字段 |
|---|---|---:|---|
| `SOL-USDT-SWAP_5m_103680_confirmed.csv` | 5m | 103,680 | `open_time_beijing, timestamp_ms, open, high, low, close, vol, volCcy, volCcyQuote, confirm` |
| `SOL-USDT-SWAP_15m_34560_confirmed.csv` | 15m | 34,560 | 同上 |
| `SOL-USDT-SWAP_30m_17280_confirmed.csv` | 30m | 17,280 | 同上 |
| `SOL-USDT-SWAP_1H_8640_confirmed.csv` | 1H | 8,640 | 同上 |
| `SOL-USDT-SWAP_2H_4320_confirmed.csv` | 2H | 4,320 | 同上 |
| `SOL-USDT-SWAP_4H_2160_confirmed.csv` | 4H | 2,160 | 同上 |

- `timestamp_ms`: Unix Epoch 毫秒（时区无关）
- `open_time_beijing`: 对应的北京时间字符串（`YYYY-MM-DD HH:MM:SS`）
- `confirm`: OKX 返回的已收盘标记（“1”表示已收盘）

资金费率（独立文件，保留最新 360 天，OKX 通常每 8 小时结算一次，约 1,080 条记录）：

| 文件 | 说明 |
|---|---|
| `SOL-USDT-SWAP_funding_rate_confirmed.csv` | `funding_time_beijing, funding_time_ms, funding_rate, instId, funding_rate_raw` |

- `funding_time_ms`: 资金费率结算时间的 Unix Epoch 毫秒（时区无关）
- `funding_time_beijing`: 对应的北京时间 ISO 8601 字符串
- `funding_rate`: 资金费率数值
- `funding_rate_raw`: OKX 返回原始记录的 JSON 字符串（可追溯）

元数据：

| 文件 | 说明 |
|---|---|
| `DATA/metadata.json` | `schema_version` 3.1，包含各 K 线粒度与资金费率的行数、起止时间、步长、去重后唯一时间戳数、区间异常数、是否全已收盘、SHA-256 校验和及每次更新的增量信息（模式/原因/新增/修订/API 请求数） |

## 更新机制

- 增量优先：本地已有数据时，仅拉取最新一批记录，与本地合并、去重、连续性校验，再裁剪为最新 360 天。
- 全量回填：本地缺失对应 CSV 时，从 OKX 回填最近 360 天。
- 原子写入：每个 CSV 先写临时文件再 `os.replace` 替换，避免部分写入。
- 4H 粒度与 5m/15m/30m/1H/2H 共享完全相同的增量/全量逻辑（`fetch_recent_confirmed` / `fetch_full_history` / `merge_recent`）。
- 资金费率使用独立增量逻辑（分页拉取 → 合并/去重 → 裁剪 360 天）。

## 触发方式

- 定时：GitHub Actions 每 6 小时运行一次（`cron: '0 */6 * * *'`）。
- 手动：在 Actions 页面使用 `workflow_dispatch` 触发。
- 本地：`python download_okx_sol_perp_klines.py`（需先 `pip install -r requirements.txt`）。

## 依赖

- Python 3.9+
- `requests`（见 `requirements.txt`）

## GitHub Secrets

OKX 公开 API 无需 API Key，脚本不依赖任何私有密钥。GitHub Actions 推送使用内置的 `secrets.GITHUB_TOKEN`（`permissions: contents: write`），无需 PM 额外配置 Secret。如需使用个人访问 Token 推送，可在仓库 Secrets 中添加并修改 workflow 中的 `token` 引用。