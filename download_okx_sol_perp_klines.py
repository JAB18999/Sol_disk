#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OKX SOL-USDT-SWAP 永续合约市场数据增量更新脚本
================================================

维护 DATA/ 下的已收盘 K 线 CSV 与资金费率 CSV，全部来自 OKX 公开 API（无需 API Key）。

K 线粒度：5m / 15m / 30m / 1H / 2H / 4H   （新增 4H）
资金费率：SOL-USDT-SWAP（通常每 8 小时结算一次）（新增独立文件）

输出文件（DATA/）：
    SOL-USDT-SWAP_5m_103680_confirmed.csv
    SOL-USDT-SWAP_15m_34560_confirmed.csv
    SOL-USDT-SWAP_30m_17280_confirmed.csv
    SOL-USDT-SWAP_1H_8640_confirmed.csv
    SOL-USDT-SWAP_2H_4320_confirmed.csv
    SOL-USDT-SWAP_4H_2160_confirmed.csv          <- 新增
    SOL-USDT-SWAP_funding_rate_confirmed.csv     <- 新增
    metadata.json

字段（K 线）：open_time_beijing, timestamp_ms, open, high, low, close, vol, volCcy, volCcyQuote, confirm
字段（资金费率）：funding_time_beijing, funding_time_ms, funding_rate, instId, funding_rate_raw

更新策略：增量优先。本地有数据时仅拉取最新一批并与本地合并、去重、连续性校验、裁剪为最新 360 天；
本地无数据时做全量回填。时区：北京时间（UTC+8）。
"""

import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "DATA")

LATEST_API = "https://www.okx.com/api/v5/market/candles"
HISTORY_API = "https://www.okx.com/api/v5/market/history-candles"
FUNDING_RATE_API = "https://www.okx.com/api/v5/public/funding-rate-history"

INST_ID = "SOL-USDT-SWAP"
BEIJING_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

DAYS_KEEP = 360
KEEP_MS = DAYS_KEEP * 24 * 60 * 60 * 1000
MAX_LIMIT = 100  # OKX 单次请求最大根数/条数

# 各粒度步长（毫秒）。新增 "4H"，其余粒度与现有逻辑完全一致，不动。
BARS: Dict[str, int] = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "2H": 2 * 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}

CSV_HEADER = [
    "open_time_beijing",
    "timestamp_ms",
    "open",
    "high",
    "low",
    "close",
    "vol",
    "volCcy",
    "volCcyQuote",
    "confirm",
]

FUNDING_HEADER = [
    "funding_time_beijing",
    "funding_time_ms",
    "funding_rate",
    "instId",
    "funding_rate_raw",
]

FUNDING_CSV_NAME = "SOL-USDT-SWAP_funding_rate_confirmed.csv"


def count_for_bar(bar: str) -> int:
    """360 天所需的根数（向上取整）。4H 返回 360 * 6 = 2160。"""
    step_ms = BARS[bar]
    return (KEEP_MS + step_ms - 1) // step_ms


def ms_to_beijing(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_okx_iso_ms(value: Optional[str]) -> Optional[int]:
    """把 OKX ISO 8601 时间字符串（如 fundingTime）解析为 Unix Epoch 毫秒。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


class OKXPublicClient:
    """OKX 公开 API 客户端（LATEST_API / HISTORY_API / FUNDING_RATE_API）。"""

    def __init__(self, timeout: int = 30, retries: int = 3, backoff: float = 2.0):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.request_count = 0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "sol-disk-data-pipeline/1.0",
                "Accept": "application/json",
            }
        )

    def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                self.request_count += 1
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.retries - 1:
                    time.sleep(self.backoff ** attempt)
        raise last_err  # type: ignore[misc]

    def fetch_candles(
        self,
        inst_id: str,
        bar: str,
        after: Optional[int] = None,
        before: Optional[int] = None,
        limit: int = MAX_LIMIT,
    ) -> List[List[str]]:
        params: Dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        data = self._get(LATEST_API, params)
        return data.get("data", [])

    def fetch_funding_rate_history(
        self,
        inst_id: str,
        after: Optional[int] = None,
        before: Optional[int] = None,
        limit: int = MAX_LIMIT,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"instId": inst_id, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        data = self._get(FUNDING_RATE_API, params)
        return data.get("data", [])


def read_kline_csv(csv_path: str) -> Tuple[Dict[int, Dict[str, str]], Optional[int], Optional[int]]:
    """读取 K 线 CSV，返回 {timestamp_ms: 行}、最小/最大 timestamp_ms。"""
    rows: Dict[int, Dict[str, str]] = {}
    if not os.path.exists(csv_path):
        return rows, None, None
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row["timestamp_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            rows[ts] = row
    if not rows:
        return rows, None, None
    return rows, min(rows), max(rows)


def write_kline_csv(csv_path: str, rows: List[Dict[str, str]]) -> None:
    """按 timestamp_ms 升序写入 K 线 CSV（原子写入：写临时文件后替换）。"""
    rows_sorted = sorted(rows, key=lambda r: int(r["timestamp_ms"]))
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows_sorted:
            writer.writerow({k: row.get(k, "") for k in CSV_HEADER})
    os.replace(tmp_path, csv_path)


def read_funding_csv(csv_path: str) -> Tuple[Dict[int, Dict[str, str]], Optional[int], Optional[int]]:
    """读取资金费率 CSV，返回 {funding_time_ms: 行}、最小/最大 funding_time_ms。"""
    rows: Dict[int, Dict[str, str]] = {}
    if not os.path.exists(csv_path):
        return rows, None, None
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = int(row["funding_time_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            rows[ts] = row
    if not rows:
        return rows, None, None
    return rows, min(rows), max(rows)


def write_funding_csv(csv_path: str, rows: List[Dict[str, str]]) -> None:
    """按 funding_time_ms 升序写入资金费率 CSV（原子写入）。"""
    rows_sorted = sorted(rows, key=lambda r: int(r["funding_time_ms"]))
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FUNDING_HEADER)
        writer.writeheader()
        for row in rows_sorted:
            writer.writerow({k: row.get(k, "") for k in FUNDING_HEADER})
    os.replace(tmp_path, csv_path)


def fetch_full_history(client: OKXPublicClient, bar: str, needed: int, end_ms: int) -> List[Dict[str, str]]:
    """全量回填：从 end_ms 向老翻页，收集 K 线，最多 needed 根。"""
    collected: List[Dict[str, str]] = []
    before: Optional[int] = end_ms
    step_ms = BARS[bar]
    while len(collected) < needed:
        page = client.fetch_candles(INST_ID, bar, after=None, before=before, limit=MAX_LIMIT)
        if not page:
            break
        page_rows: List[Dict[str, str]] = []
        for candle in page:
            if len(candle) < 9:
                continue
            ts = int(candle[0])
            row = {
                "open_time_beijing": ms_to_beijing(ts),
                "timestamp_ms": str(ts),
                "open": candle[1],
                "high": candle[2],
                "low": candle[3],
                "close": candle[4],
                "vol": candle[5],
                "volCcy": candle[6],
                "volCcyQuote": candle[7],
                "confirm": candle[8],
            }
            page_rows.append(row)
        if not page_rows:
            break
        collected.extend(page_rows)
        oldest_ts = min(int(r["timestamp_ms"]) for r in page_rows)
        before = oldest_ts
        if len(page_rows) < MAX_LIMIT:
            break
    uniq: Dict[int, Dict[str, str]] = {}
    for r in collected:
        uniq[int(r["timestamp_ms"])] = r
    ordered = sorted(uniq.values(), key=lambda r: int(r["timestamp_ms"]), reverse=True)
    return ordered[:needed]


def fetch_recent_confirmed(
    client: OKXPublicClient, bar: str, since_ms: int, needed: int
) -> List[Dict[str, str]]:
    """增量：拉取 since_ms 之后的 K 线（最新页起向老翻页），返回新增行。"""
    collected: List[Dict[str, str]] = []
    before: Optional[int] = None
    while True:
        page = client.fetch_candles(INST_ID, bar, after=None, before=before, limit=MAX_LIMIT)
        if not page:
            break
        stop = False
        for candle in page:
            if len(candle) < 9:
                continue
            ts = int(candle[0])
            if ts <= since_ms:
                stop = True
                break
            row = {
                "open_time_beijing": ms_to_beijing(ts),
                "timestamp_ms": str(ts),
                "open": candle[1],
                "high": candle[2],
                "low": candle[3],
                "close": candle[4],
                "vol": candle[5],
                "volCcy": candle[6],
                "volCcyQuote": candle[7],
                "confirm": candle[8],
            }
            collected.append(row)
        if stop:
            break
        valid_ts = [int(c[0]) for c in page if len(c) >= 9]
        if not valid_ts:
            break
        before = min(valid_ts)
        if len(page) < MAX_LIMIT:
            break
        if len(collected) >= needed:
            break
    return collected


def merge_recent(
    existing: Dict[int, Dict[str, str]], recent: List[Dict[str, str]]
) -> Tuple[Dict[int, Dict[str, str]], int, int]:
    """将最近抓取的 K 线合并到已有数据。
    返回 (合并后字典, 新增根数, 修订根数)。API 数据对已有同 timestamp 行进行覆盖（已收盘补齐 confirm 等）。
    """
    merged = dict(existing)
    new_candles = 0
    revised_candles = 0
    for row in recent:
        ts = int(row["timestamp_ms"])
        if ts not in merged:
            merged[ts] = row
            new_candles += 1
        else:
            old = merged[ts]
            if any(old.get(k) != row.get(k) for k in CSV_HEADER):
                merged[ts] = row
                revised_candles += 1
    return merged, new_candles, revised_candles


def trim_to_latest(rows: Dict[int, Dict[str, str]], needed: int) -> List[Dict[str, str]]:
    ordered = sorted(rows.values(), key=lambda r: int(r["timestamp_ms"]), reverse=True)
    return ordered[:needed]


def update_timeframe(client: OKXPublicClient, bar: str) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """处理单个粒度的增量/全量更新，返回 (metadata update 信息, 最终写入行)。4H 与其余粒度共享同一逻辑。"""
    step_ms = BARS[bar]
    needed = count_for_bar(bar)
    csv_name = f"SOL-USDT-SWAP_{bar}_{needed}_confirmed.csv"
    csv_path = os.path.join(DATA_DIR, csv_name)

    existing, min_ts, max_ts = read_kline_csv(csv_path)
    now_ms = int(time.time() * 1000)
    requests_before = client.request_count

    update_info: Dict[str, Any] = {
        "bar": bar,
        "csv_filename": csv_name,
    }

    if not existing:
        rows = fetch_full_history(client, bar, needed, now_ms)
        update_info["mode"] = "full"
        update_info["reason"] = "local_csv_missing"
        update_info["new_candles"] = len(rows)
        update_info["revised_candles"] = 0
        final_rows = trim_to_latest({int(r["timestamp_ms"]): r for r in rows}, needed)
    else:
        since_ms = max_ts if max_ts is not None else 0
        recent = fetch_recent_confirmed(client, bar, since_ms, needed)
        merged, new_candles, revised = merge_recent(existing, recent)
        update_info["mode"] = "incremental"
        update_info["reason"] = "local_csv_exists"
        update_info["new_candles"] = new_candles
        update_info["revised_candles"] = revised
        final_rows = trim_to_latest(merged, needed)

    update_info["api_requests"] = client.request_count - requests_before
    write_kline_csv(csv_path, final_rows)
    return update_info, final_rows


def fetch_funding_recent(
    client: OKXPublicClient, since_ms: int, needed_limit: Optional[int] = None
) -> List[Dict[str, str]]:
    """增量：拉取 since_ms 之后的资金费率记录（最新页起向老翻页）。"""
    collected: List[Dict[str, str]] = []
    before: Optional[int] = None
    while True:
        page = client.fetch_funding_rate_history(INST_ID, after=None, before=before, limit=MAX_LIMIT)
        if not page:
            break
        stop = False
        for item in page:
            ft_ms = parse_okx_iso_ms(item.get("fundingTime"))
            if ft_ms is None:
                continue
            if ft_ms <= since_ms:
                stop = True
                break
            row = {
                "funding_time_beijing": ms_to_beijing(ft_ms),
                "funding_time_ms": str(ft_ms),
                "funding_rate": item.get("fundingRate", ""),
                "instId": item.get("instId", INST_ID),
                "funding_rate_raw": json.dumps(item, ensure_ascii=False),
            }
            collected.append(row)
            if needed_limit is not None and len(collected) >= needed_limit:
                stop = True
                break
        if stop:
            break
        valid_ts = [
            parse_okx_iso_ms(it.get("fundingTime")) for it in page if parse_okx_iso_ms(it.get("fundingTime")) is not None
        ]
        if not valid_ts:
            break
        before = min(valid_ts)
        if len(page) < MAX_LIMIT:
            break
    return collected


def fetch_funding_full(client: OKXPublicClient, end_ms: Optional[int] = None) -> List[Dict[str, str]]:
    """全量回填资金费率：从 end_ms 向老翻页，保留最近 360 天记录。"""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    cutoff_ms = end_ms - KEEP_MS
    collected: List[Dict[str, str]] = []
    before: Optional[int] = end_ms
    while True:
        page = client.fetch_funding_rate_history(INST_ID, after=None, before=before, limit=MAX_LIMIT)
        if not page:
            break
        page_rows: List[Dict[str, str]] = []
        oldest_valid: Optional[int] = None
        for item in page:
            ft_ms = parse_okx_iso_ms(item.get("fundingTime"))
            if ft_ms is None:
                continue
            if ft_ms < cutoff_ms:
                continue
            row = {
                "funding_time_beijing": ms_to_beijing(ft_ms),
                "funding_time_ms": str(ft_ms),
                "funding_rate": item.get("fundingRate", ""),
                "instId": item.get("instId", INST_ID),
                "funding_rate_raw": json.dumps(item, ensure_ascii=False),
            }
            page_rows.append(row)
            if oldest_valid is None or ft_ms < oldest_valid:
                oldest_valid = ft_ms
        collected.extend(page_rows)
        if oldest_valid is None:
            break
        before = oldest_valid
        if len(page) < MAX_LIMIT:
            break
    uniq: Dict[int, Dict[str, str]] = {}
    for item in collected:
        ft_ms = int(item["funding_time_ms"])
        uniq[ft_ms] = item
    cutoff = end_ms - KEEP_MS
    ordered = sorted(uniq.values(), key=lambda r: int(r["funding_time_ms"]), reverse=True)
    kept = [r for r in ordered if int(r["funding_time_ms"]) >= cutoff]
    return kept


def merge_funding(
    existing: Dict[int, Dict[str, str]], recent: List[Dict[str, str]]
) -> Tuple[Dict[int, Dict[str, str]], int, int]:
    merged = dict(existing)
    new_candles = 0
    revised_candles = 0
    for row in recent:
        ts = int(row["funding_time_ms"])
        if ts not in merged:
            merged[ts] = row
            new_candles += 1
        else:
            if merged[ts].get("funding_rate") != row.get("funding_rate"):
                merged[ts] = row
                revised_candles += 1
    return merged, new_candles, revised_candles


def trim_funding_to_360d(rows: Dict[int, Dict[str, str]], end_ms: int) -> List[Dict[str, str]]:
    cutoff = end_ms - KEEP_MS
    ordered = sorted(rows.values(), key=lambda r: int(r["funding_time_ms"]), reverse=True)
    return [r for r in ordered if int(r["funding_time_ms"]) >= cutoff]


def update_funding_rate(client: OKXPublicClient) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """资金费率增量/全量更新，返回 (metadata update 信息, 最终写入行)。"""
    csv_path = os.path.join(DATA_DIR, FUNDING_CSV_NAME)
    existing, min_ts, max_ts = read_funding_csv(csv_path)
    now_ms = int(time.time() * 1000)
    requests_before = client.request_count

    update_info: Dict[str, Any] = {
        "instId": INST_ID,
        "csv_filename": FUNDING_CSV_NAME,
    }

    if not existing:
        rows = fetch_funding_full(client, now_ms)
        update_info["mode"] = "full"
        update_info["reason"] = "local_csv_missing"
        update_info["new_candles"] = len(rows)
        update_info["revised_candles"] = 0
        final_rows = trim_funding_to_360d({int(r["funding_time_ms"]): r for r in rows}, now_ms)
    else:
        since_ms = max_ts if max_ts is not None else 0
        recent = fetch_funding_recent(client, since_ms)
        merged, new_candles, revised = merge_funding(existing, recent)
        update_info["mode"] = "incremental"
        update_info["reason"] = "local_csv_exists"
        update_info["new_candles"] = new_candles
        update_info["revised_candles"] = revised
        final_rows = trim_funding_to_360d(merged, now_ms)

    update_info["api_requests"] = client.request_count - requests_before
    write_funding_csv(csv_path, final_rows)
    return update_info, final_rows


def build_metadata(
    kline_results: Dict[str, Tuple[Dict[str, Any], List[Dict[str, str]]]],
    funding_update: Dict[str, Any],
    funding_rows: List[Dict[str, str]],
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    meta: Dict[str, Any] = {
        "schema_version": "3.1",
        "instrument": INST_ID,
        "generated_at_beijing": ms_to_beijing(now_ms),
        "generated_at_utc_ms": now_ms,
        "timezone": "Beijing (UTC+8)",
        "anchor_timeframe": "2H",
        "days_keep": DAYS_KEEP,
        "timeframes": {},
        "funding_rate": {},
    }

    for bar in BARS:
        update_info, final_rows = kline_results[bar]
        needed = count_for_bar(bar)
        csv_name = f"SOL-USDT-SWAP_{bar}_{needed}_confirmed.csv"
        csv_path = os.path.join(DATA_DIR, csv_name)
        ts_list = sorted(int(r["timestamp_ms"]) for r in final_rows)
        step = BARS[bar]
        anomalies = 0
        for a, b in zip(ts_list, ts_list[1:]):
            if b - a != step:
                anomalies += 1
        all_confirmed = all(r.get("confirm") == "1" for r in final_rows)
        entry: Dict[str, Any] = {
            "bar": bar,
            "row_count": len(final_rows),
            "start_timestamp_ms": ts_list[0] if ts_list else None,
            "end_timestamp_ms": ts_list[-1] if ts_list else None,
            "start_beijing": ms_to_beijing(ts_list[0]) if ts_list else None,
            "end_beijing": ms_to_beijing(ts_list[-1]) if ts_list else None,
            "expected_step_ms": step,
            "unique_timestamp_count": len(set(ts_list)),
            "interval_anomaly_count": anomalies,
            "all_confirmed": all_confirmed,
            "csv_filename": csv_name,
            "update": update_info,
            "csv_sha256": sha256_file(csv_path) if os.path.exists(csv_path) else None,
        }
        meta["timeframes"][bar] = entry

    f_ts = sorted(int(r["funding_time_ms"]) for r in funding_rows)
    funding_csv_path = os.path.join(DATA_DIR, FUNDING_CSV_NAME)
    meta["funding_rate"] = {
        "instId": INST_ID,
        "row_count": len(funding_rows),
        "start_timestamp_ms": f_ts[0] if f_ts else None,
        "end_timestamp_ms": f_ts[-1] if f_ts else None,
        "start_beijing": ms_to_beijing(f_ts[0]) if f_ts else None,
        "end_beijing": ms_to_beijing(f_ts[-1]) if f_ts else None,
        "unique_timestamp_count": len(set(f_ts)),
        "csv_filename": FUNDING_CSV_NAME,
        "update": funding_update,
        "csv_sha256": sha256_file(funding_csv_path) if os.path.exists(funding_csv_path) else None,
    }
    return meta


def main() -> int:
    ensure_data_dir()
    client = OKXPublicClient()

    kline_results: Dict[str, Tuple[Dict[str, Any], List[Dict[str, str]]]] = {}
    for bar in BARS:
        update_info, final_rows = update_timeframe(client, bar)
        kline_results[bar] = (update_info, final_rows)
        print(
            f"[KLINE] {bar}: mode={update_info['mode']} "
            f"new={update_info['new_candles']} revised={update_info['revised_candles']} "
            f"rows={len(final_rows)} file={update_info['csv_filename']}"
        )

    funding_update, funding_rows = update_funding_rate(client)
    print(
        f"[FUNDING] mode={funding_update['mode']} "
        f"new={funding_update['new_candles']} revised={funding_update['revised_candles']} "
        f"rows={len(funding_rows)} file={funding_update['csv_filename']}"
    )

    meta = build_metadata(kline_results, funding_update, funding_rows)
    meta_path = os.path.join(DATA_DIR, "metadata.json")
    tmp_meta = meta_path + ".tmp"
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp_meta, meta_path)
    print(f"[META] wrote {meta_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
