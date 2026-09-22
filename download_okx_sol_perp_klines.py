#!/usr/bin/env python3
"""Incrementally maintain OKX SOL-USDT-SWAP OHLCV CSV files in Beijing time.

Normal mode reads the repository's existing CSV files, retrieves only a recent
300-bar overlap from OKX for each timeframe, merges newly closed candles,
validates continuity, and retains the latest 180 days of completed candles for each timeframe.

All human-readable timestamps written by this script use Asia/Shanghai
(Beijing time, UTC+08:00). `timestamp_ms` remains a Unix-epoch millisecond
identifier: it is timezone-independent and must not be shifted by eight hours.

If a CSV is missing, invalid, or too far behind the recent API window, the
script safely falls back to a full 180-day historical backfill. It uses a
single-process lock, conservative request pacing, bounded retries, 429-aware
backoff, and atomic output replacement.

Examples:
    python3 download_okx_sol_perp_klines.py
    python3 download_okx_sol_perp_klines.py --full-refresh
    python3 download_okx_sol_perp_klines.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
INSTRUMENT = "SOL-USDT-SWAP"
RETENTION_DAYS = 180
RECENT_LIMIT = 300
BEIJING_TIMEZONE_NAME = "Asia/Shanghai"
BEIJING_TIMEZONE = ZoneInfo(BEIJING_TIMEZONE_NAME)
# Conservative process-wide pace: 4 requests/sec, well below the documented
# history-candle limit of 20 requests/2 seconds. This includes retries.
MIN_REQUEST_INTERVAL_SECONDS = 0.25
MAX_RETRIES = 6
LOCK_FILE = ROOT / ".okx_candle_update.lock"
LOCK_STALE_SECONDS = 30 * 60

BARS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "2H": 2 * 60 * 60 * 1000,
}
ROWS_BY_BAR = {
    bar: RETENTION_DAYS * 24 * 60 * 60 * 1000 // step_ms
    for bar, step_ms in BARS.items()
}


def csv_filename(bar: str) -> str:
    return f"{INSTRUMENT}_{bar}_{RETENTION_DAYS}d_confirmed.csv"
LATEST_API = "https://www.okx.com/api/v5/market/candles"
HISTORY_API = "https://www.okx.com/api/v5/market/history-candles"
HEADERS = {
    # Keep a stable, identifiable client name. Do not rotate user agents or IPs.
    "User-Agent": "Mozilla/5.0 (compatible; SOL-disk-incremental-updater/1.1; +https://github.com/JAB18999/Sol_disk)",
    "Accept": "application/json",
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
# Read-only migration support for the schema committed before Beijing-time
# unification. Legacy rows are rewritten with the new single display column.
LEGACY_CSV_HEADER = [
    "open_time_utc",
    "open_time_shanghai",
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
RATE_LIMIT_CODES = {"50011", "50040", "50061"}


class DataValidationError(RuntimeError):
    """Raised when a local or API candle series is not fit for use."""


class FatalAPIError(RuntimeError):
    """Raised for requests that should not be retried automatically."""


class RetryableAPIError(RuntimeError):
    """Raised for transient errors, including rate limiting."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RequestPacer:
    """A tiny single-process leaky-bucket style request pacer."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.next_request_at = 0.0

    def wait_turn(self) -> None:
        now = time.monotonic()
        if now < self.next_request_at:
            time.sleep(self.next_request_at - now)
        self.next_request_at = max(now, self.next_request_at) + self.min_interval_seconds


class UpdateLock:
    """Prevent overlapping scheduled/manual updater runs in one checkout."""

    def __init__(self, path: Path, stale_seconds: int = LOCK_STALE_SECONDS) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.held = False

    def __enter__(self) -> "UpdateLock":
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as lock:
                    lock.write(json.dumps({"pid": os.getpid(), "started_at_beijing": beijing_now()}, ensure_ascii=False))
                self.held = True
                return self
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if attempt == 0 and age > self.stale_seconds:
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(
                    f"Another update appears to be running ({self.path.name}, age={age:.0f}s). "
                    "Refusing to make concurrent API requests or overwrite data."
                )
        raise RuntimeError("Unable to acquire update lock")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def beijing_now() -> str:
    """Return a timezone-explicit Beijing timestamp for all human-readable metadata."""
    return datetime.now(timezone.utc).astimezone(BEIJING_TIMEZONE).isoformat()


def beijing_time(timestamp_ms: int) -> str:
    """Render an absolute Unix timestamp in Asia/Shanghai without altering the epoch value."""
    instant = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return instant.astimezone(BEIJING_TIMEZONE).isoformat()


def parse_retry_after(headers: Any) -> float | None:
    """Parse a numeric Retry-After header when an API provides one."""
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class OKXPublicClient:
    """Public API client with conservative pacing and error-aware retries."""

    def __init__(self) -> None:
        self.pacer = RequestPacer(MIN_REQUEST_INTERVAL_SECONDS)
        self.request_count = 0

    def get_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            self.pacer.wait_turn()
            self.request_count += 1
            request = urllib.request.Request(url, headers=HEADERS)
            retry_after: float | None = None
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))

                if payload.get("code") == "0":
                    return payload

                code = str(payload.get("code", ""))
                message = str(payload.get("msg", ""))
                if code in RATE_LIMIT_CODES or "too frequent" in message.lower() or "rate limit" in message.lower():
                    raise RetryableAPIError(f"OKX rate-limited this request: code={code}, msg={message}")
                raise FatalAPIError(f"OKX rejected request: code={code}, msg={message}")

            except urllib.error.HTTPError as exc:
                retry_after = parse_retry_after(exc.headers)
                if exc.code in {400, 401, 403, 404}:
                    raise FatalAPIError(
                        f"HTTP {exc.code}; not retrying because parameters/access should be reviewed."
                    ) from exc
                if exc.code == 429 or 500 <= exc.code <= 599 or exc.code in {408, 409}:
                    last_error = RetryableAPIError(f"HTTP {exc.code}", retry_after)
                else:
                    raise FatalAPIError(f"HTTP {exc.code}; not retrying automatically.") from exc
            except RetryableAPIError as exc:
                last_error = exc
                retry_after = exc.retry_after_seconds
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt == MAX_RETRIES - 1:
                break

            # Full-jitter exponential backoff avoids synchronised retry bursts.
            exponential_cap = min(60.0, 1.0 * (2 ** attempt))
            delay = random.uniform(0.5, exponential_cap)
            if retry_after is not None:
                delay = max(delay, retry_after)
            print(
                f"Transient API error ({type(last_error).__name__}: {last_error}); "
                f"retrying in {delay:.2f}s [{attempt + 1}/{MAX_RETRIES - 1}]",
                flush=True,
            )
            time.sleep(delay)

        raise RetryableAPIError(f"OKX request failed after {MAX_RETRIES} attempts: {last_error}")


def api_candle_to_row(candle: list[str]) -> dict[str, str]:
    if len(candle) < 9:
        raise DataValidationError(f"Unexpected OKX candle schema: {candle!r}")
    ts, open_price, high, low, close, vol, vol_ccy, vol_ccy_quote, confirm = candle[:9]
    timestamp_ms = int(ts)
    return {
        "open_time_beijing": beijing_time(timestamp_ms),
        "timestamp_ms": str(timestamp_ms),
        "open": str(open_price),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "vol": str(vol),
        "volCcy": str(vol_ccy),
        "volCcyQuote": str(vol_ccy_quote),
        "confirm": str(confirm),
    }


def fetch_recent_confirmed(client: OKXPublicClient, bar: str) -> list[dict[str, str]]:
    """Fetch a recent overlap window; normally this is the whole incremental API cost."""
    response = client.get_json(
        LATEST_API,
        {"instId": INSTRUMENT, "bar": bar, "limit": str(RECENT_LIMIT)},
    )
    rows = [api_candle_to_row(candle) for candle in response.get("data", []) if len(candle) >= 9 and candle[8] == "1"]
    if not rows:
        raise DataValidationError(f"OKX returned no completed recent {bar} candles")
    return sorted(rows, key=lambda row: int(row["timestamp_ms"]))


def fetch_full_history(client: OKXPublicClient, bar: str, count: int) -> list[dict[str, str]]:
    """Backfill the requested latest confirmed candles using historical pagination."""
    newest_first: list[list[str]] = []
    seen: set[int] = set()
    cursor: int | None = None

    while len(newest_first) < count:
        params = {"instId": INSTRUMENT, "bar": bar, "limit": str(RECENT_LIMIT)}
        if cursor is not None:
            # OKX history-candles `after` returns candles strictly older than cursor.
            params["after"] = str(cursor)
        response = client.get_json(HISTORY_API, params)
        page = response.get("data", [])
        if not page:
            raise DataValidationError(f"OKX returned no more historical {bar} candles at cursor={cursor}")

        timestamps = [int(candle[0]) for candle in page if candle]
        if not timestamps:
            raise DataValidationError(f"OKX returned malformed historical {bar} page")
        if cursor is not None and max(timestamps) >= cursor:
            raise DataValidationError(f"Non-decreasing history pagination for {bar}")

        for candle in page:
            if len(candle) >= 9 and candle[8] == "1" and int(candle[0]) not in seen:
                newest_first.append(candle)
                seen.add(int(candle[0]))
        cursor = min(timestamps)

    rows = [api_candle_to_row(candle) for candle in newest_first[:count]]
    return sorted(rows, key=lambda row: int(row["timestamp_ms"]))


def read_existing_csv(path: Path, expected_step_ms: int, expected_count: int) -> tuple[list[dict[str, str]], bool]:
    """Read the current schema or migrate a legacy UTC/Shanghai display schema in memory."""
    if not path.exists():
        raise DataValidationError(f"Missing file: {path.name}")

    try:
        with path.open("r", newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames
            if fieldnames == CSV_HEADER:
                rows = [{column: row[column] for column in CSV_HEADER} for row in reader]
                migrated_legacy_schema = False
            elif fieldnames == LEGACY_CSV_HEADER:
                rows = []
                for old_row in reader:
                    timestamp_ms = int(old_row["timestamp_ms"])
                    rows.append(
                        {
                            "open_time_beijing": beijing_time(timestamp_ms),
                            "timestamp_ms": str(timestamp_ms),
                            "open": old_row["open"],
                            "high": old_row["high"],
                            "low": old_row["low"],
                            "close": old_row["close"],
                            "vol": old_row["vol"],
                            "volCcy": old_row["volCcy"],
                            "volCcyQuote": old_row["volCcyQuote"],
                            "confirm": old_row["confirm"],
                        }
                    )
                migrated_legacy_schema = True
            else:
                raise DataValidationError(f"Unexpected CSV header in {path.name}: {fieldnames}")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DataValidationError(f"Unable to read a valid CSV schema from {path.name}") from exc

    validate_rows(rows, expected_step_ms, expected_count)
    return rows, migrated_legacy_schema


def validate_rows(rows: list[dict[str, str]], expected_step_ms: int, expected_count: int) -> None:
    if len(rows) != expected_count:
        raise DataValidationError(f"Expected {expected_count} rows, received {len(rows)}")

    timestamps: list[int] = []
    for row in rows:
        if row.get("confirm") != "1":
            raise DataValidationError("Dataset contains an unconfirmed candle")
        try:
            timestamp = int(row["timestamp_ms"])
            expected_beijing = beijing_time(timestamp)
            if row["open_time_beijing"] != expected_beijing:
                raise DataValidationError(
                    f"Beijing display time disagrees with timestamp_ms at {row['timestamp_ms']}"
                )
            # Validate numeric fields without changing the raw API values.
            for field in ("open", "high", "low", "close", "vol", "volCcy", "volCcyQuote"):
                float(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataValidationError(f"Invalid row around timestamp {row.get('timestamp_ms')}") from exc
        timestamps.append(timestamp)

    if len(set(timestamps)) != expected_count:
        raise DataValidationError("Duplicate candle timestamps")
    if timestamps != sorted(timestamps):
        raise DataValidationError("Candle timestamps are not sorted ascending")
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous != expected_step_ms:
            raise DataValidationError(
                f"Candle interval gap: {previous} -> {current}; expected {expected_step_ms}ms"
            )


def row_signature(rows: list[dict[str, str]]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(row[column] for column in CSV_HEADER) for row in rows)


def merge_recent(
    existing: list[dict[str, str]],
    recent: list[dict[str, str]],
    expected_step_ms: int,
    expected_count: int,
) -> tuple[list[dict[str, str]], int, int, str]:
    """Merge the recent overlap and detect whether a full fallback is required."""
    existing_last = int(existing[-1]["timestamp_ms"])
    recent_first = int(recent[0]["timestamp_ms"])
    recent_last = int(recent[-1]["timestamp_ms"])

    if recent_last < existing_last:
        # Do not overwrite a newer local dataset with a temporarily stale API response.
        return existing, 0, 0, "recent_api_older_than_local_skip"

    # If the local tail is older than the earliest returned recent bar, the normal
    # 300-bar overlap cannot prove/fill the missing range. Backfill safely instead.
    if existing_last < recent_first - expected_step_ms:
        raise DataValidationError("local_tail_outside_recent_api_window")

    existing_by_ts = {int(row["timestamp_ms"]): row for row in existing}
    new_count = 0
    revised_count = 0
    for row in recent:
        timestamp = int(row["timestamp_ms"])
        old = existing_by_ts.get(timestamp)
        if old is None:
            new_count += 1
        elif tuple(old[column] for column in CSV_HEADER) != tuple(row[column] for column in CSV_HEADER):
            revised_count += 1
        existing_by_ts[timestamp] = row

    merged = [existing_by_ts[timestamp] for timestamp in sorted(existing_by_ts)]
    latest_window = merged[-expected_count:]
    validate_rows(latest_window, expected_step_ms, expected_count)
    return latest_window, new_count, revised_count, "incremental_recent_overlap"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temporary:
        writer = csv.DictWriter(temporary, fieldnames=CSV_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_name = temporary.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_name = temporary.name
    os.replace(temp_name, path)


@dataclass
class UpdateResult:
    bar: str
    rows: list[dict[str, str]]
    mode: str
    reason: str
    changed: bool
    new_candles: int
    revised_candles: int
    api_requests: int


def update_timeframe(client: OKXPublicClient, bar: str, expected_step_ms: int, force_full: bool) -> UpdateResult:
    expected_count = ROWS_BY_BAR[bar]
    path = ROOT / csv_filename(bar)
    before_requests = client.request_count
    existing: list[dict[str, str]] | None = None
    fallback_reason: str | None = None

    if not force_full:
        try:
            existing, legacy_schema = read_existing_csv(path, expected_step_ms, expected_count)
            recent = fetch_recent_confirmed(client, bar)
            candidate, new_count, revised_count, reason = merge_recent(existing, recent, expected_step_ms, expected_count)
            changed = legacy_schema or row_signature(candidate) != row_signature(existing)
            if legacy_schema and reason == "incremental_recent_overlap":
                reason = "legacy_timezone_schema_migration_plus_recent_overlap"
            return UpdateResult(
                bar=bar,
                rows=candidate,
                mode="incremental",
                reason=reason,
                changed=changed,
                new_candles=new_count,
                revised_candles=revised_count,
                api_requests=client.request_count - before_requests,
            )
        except DataValidationError as exc:
            fallback_reason = str(exc)
            print(f"{bar}: incremental path unavailable ({fallback_reason}); using full historical backfill.", flush=True)

    if force_full:
        fallback_reason = "forced_full_refresh"

    full_rows = fetch_full_history(client, bar, expected_count)
    validate_rows(full_rows, expected_step_ms, expected_count)
    changed = existing is None or row_signature(full_rows) != row_signature(existing)
    old_timestamps = {int(row["timestamp_ms"]) for row in existing} if existing else set()
    new_count = sum(1 for row in full_rows if int(row["timestamp_ms"]) not in old_timestamps)
    revised_count = 0
    if existing:
        old_by_ts = {int(row["timestamp_ms"]): row for row in existing}
        revised_count = sum(
            1
            for row in full_rows
            if int(row["timestamp_ms"]) in old_by_ts
            and tuple(row[column] for column in CSV_HEADER)
            != tuple(old_by_ts[int(row["timestamp_ms"])][column] for column in CSV_HEADER)
        )

    return UpdateResult(
        bar=bar,
        rows=full_rows,
        mode="full_backfill",
        reason=fallback_reason or "full_backfill",
        changed=changed,
        new_candles=new_count,
        revised_candles=revised_count,
        api_requests=client.request_count - before_requests,
    )


def read_previous_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_metadata(results: list[UpdateResult], previous_metadata: dict[str, Any]) -> dict[str, Any]:
    timeframes: dict[str, Any] = {}
    for result in results:
        step_ms = BARS[result.bar]
        timestamps = [int(row["timestamp_ms"]) for row in result.rows]
        timeframes[result.bar] = {
            "bar": result.bar,
            "row_count": len(result.rows),
            "start_timestamp_ms": str(timestamps[0]),
            "end_timestamp_ms": str(timestamps[-1]),
            "start_time_beijing": result.rows[0]["open_time_beijing"],
            "end_time_beijing": result.rows[-1]["open_time_beijing"],
            "expected_step_ms": step_ms,
            "unique_timestamp_count": len(set(timestamps)),
            "interval_anomaly_count": 0,
            "all_confirmed": True,
            "csv_filename": csv_filename(result.bar),
            "update": {
                "mode": result.mode,
                "reason": result.reason,
                "new_candles": result.new_candles,
                "revised_candles": result.revised_candles,
                "api_requests": result.api_requests,
            },
        }

    metadata: dict[str, Any] = {
        "schema_version": "3.0",
        "instrument": INSTRUMENT,
        "instrument_type": "USDT-margined perpetual swap",
        "timezone": {
            "name": BEIJING_TIMEZONE_NAME,
            "offset": "+08:00",
            "human_readable_timestamp_column": "open_time_beijing",
            "note": "All human-readable timestamps in this repository are Beijing time. timestamp_ms is a timezone-independent Unix epoch value.",
        },
        "source_endpoints": {
            "incremental": LATEST_API,
            "bootstrap_or_gap_recovery": HISTORY_API,
        },
        "completed_candles_only": True,
        "sort_order": "ascending by candle open timestamp; human-readable display is Asia/Shanghai",
        "retention_days": RETENTION_DAYS,
        "requested_rows_by_timeframe": ROWS_BY_BAR,
        "recent_overlap_limit": RECENT_LIMIT,
        "rate_limit_policy": {
            "minimum_request_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
            "max_retries": MAX_RETRIES,
            "note": "Single-process pacing, Retry-After-aware retry, and no proxy/user-agent rotation.",
        },
        "last_successful_data_update_beijing": beijing_now(),
        "timeframes": timeframes,
    }
    # Preserve the original instrument snapshot if a prior full-download metadata file has one.
    if previous_metadata.get("instrument_snapshot"):
        metadata["instrument_snapshot"] = previous_metadata["instrument_snapshot"]
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally update OKX SOL-USDT-SWAP candle CSV data in Beijing time.")
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Ignore local CSV files and re-download the latest 180 days per timeframe.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, merge, and validate without writing CSV or metadata files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    metadata_path = ROOT / "metadata.json"

    with UpdateLock(LOCK_FILE):
        previous_metadata = read_previous_metadata(metadata_path)
        client = OKXPublicClient()
        results: list[UpdateResult] = []

        for bar, step_ms in BARS.items():
            print(f"Updating {bar} ...", flush=True)
            result = update_timeframe(client, bar, step_ms, args.full_refresh)
            results.append(result)
            print(
                f"{bar}: mode={result.mode}, changed={result.changed}, "
                f"new={result.new_candles}, revised={result.revised_candles}, "
                f"requests={result.api_requests}, reason={result.reason}",
                flush=True,
            )

        data_changed = any(result.changed for result in results)
        metadata_needs_migration = previous_metadata.get("schema_version") != "3.0"
        should_write_metadata = data_changed or metadata_needs_migration

        if args.dry_run:
            print("Dry run complete: no files were written.", flush=True)
            return 0

        for result in results:
            if result.changed:
                atomic_write_csv(ROOT / csv_filename(result.bar), result.rows)

        if should_write_metadata:
            metadata = build_metadata(results, previous_metadata)
            for result in results:
                csv_path = ROOT / csv_filename(result.bar)
                metadata["timeframes"][result.bar]["csv_sha256"] = sha256(csv_path)
            atomic_write_json(metadata_path, metadata)

        if data_changed:
            print("Update complete: Beijing-time datasets and metadata were written atomically.", flush=True)
        elif metadata_needs_migration:
            print("No candle changes; Beijing-time metadata schema was migrated.", flush=True)
        else:
            print("No new or revised completed candles; repository data was left unchanged.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
