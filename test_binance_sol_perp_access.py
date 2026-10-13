#!/usr/bin/env python3
"""Probe whether the runtime (GitHub Actions runner or local machine) can
reach the Binance USDT-margined perpetual (futures) market data API and
download SOL-USDT candles.

This is a connectivity / data-source sanity test, not a production downloader.
It only fetches a small number of confirmed candles and prints a verifiable
summary: the exact endpoint used, the HTTP status, the returned symbol, the
candle count, the first/last timestamps, and a sample OHLC row.

Binance market endpoints probed
    USDT-M perpetual (futures): https://fapi.binance.com/fapi/v1/klines
        (also tried: fapi1 / fapi2 / fapi3 / fapi4 .binance.com)
    Spot (reference only):      https://api.binance.com/api/v3/klines

Symbol mapping
    SOL-USDT-SWAP in OKX notation  ->  SOLUSDT on Binance (USDT-margined
    perpetual contract under the /fapi market).

Exit codes
    0  at least one futures endpoint returned valid SOLUSDT candles
    1  all probes failed

Run
    python3 test_binance_sol_perp_access.py
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SYMBOL = "SOLUSDT"
INTERVAL = "15m"
LIMIT = 10  # only probe a tiny window; this is a connectivity test
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

# USDT-margined perpetual futures endpoints (primary + regional mirrors).
FUTURES_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
]
# Spot endpoint is probed only to confirm that Binance's general API is
# reachable when the futures domain is blocked; it does NOT supply
# perpetual data and is never used as a fallback for futures candles.
SPOT_ENDPOINT = "https://api.binance.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; binance-sol-perp-probe/1.0)",
    "Accept": "application/json",
}
MAX_RETRIES = 3


def beijing_time(timestamp_ms: int) -> str:
    instant = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return instant.astimezone(BEIJING_TIMEZONE).isoformat()


def fetch_json(base_url: str, params: dict[str, str], market_type: str) -> dict | list | None:
    """Fetch JSON from one Binance endpoint; return parsed body or None on failure."""
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    for attempt in range(MAX_RETRIES):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            try:
                detail += f" {exc.read().decode('utf-8')[:200]}"
            except Exception:
                pass
            print(f"  [{market_type}] {base_url}: {detail}")
            if exc.code in {400, 401, 403, 404, 429}:
                return None  # do not retry parameter/auth/limit errors
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"  [{market_type}] {base_url}: {type(exc).__name__}: {exc}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(random.uniform(0.5, 2.0))
    return None


def probe_futures(base_url: str) -> bool:
    """Try to fetch SOLUSDT perpetual candles from one futures endpoint."""
    payload = fetch_json(
        base_url,
        {"symbol": SYMBOL, "interval": INTERVAL, "limit": str(LIMIT)},
        market_type="futures",
    )
    if not isinstance(payload, list) or not payload:
        return False
    # Binance futures klines row schema:
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume,
    #  trades, takerBuyBaseVol, takerBuyQuoteVol, ignore]
    first = payload[0]
    last = payload[-1]
    if len(first) < 6:
        print(f"  [futures] {base_url}: unexpected row schema {first!r}")
        return False
    open_time = int(first[0])
    close_time = int(last[6])
    print(f"  [futures] {base_url}: OK")
    print(f"      symbol       : {SYMBOL} (USDT-margined perpetual)")
    print(f"      interval     : {INTERVAL}")
    print(f"      candles      : {len(payload)}")
    print(f"      first open   : {beijing_time(open_time)}  ({first[0]})")
    print(f"      last close   : {beijing_time(close_time)}  ({last[6]})")
    sample = {
        "open_time_beijing": beijing_time(open_time),
        "open": first[1],
        "high": first[2],
        "low": first[3],
        "close": first[4],
        "vol": first[5],
    }
    print(f"      sample candle: {json.dumps(sample, ensure_ascii=False)}")
    return True


def probe_spot() -> None:
    """Probe the spot endpoint as a reachability reference only."""
    payload = fetch_json(
        SPOT_ENDPOINT,
        {"symbol": SYMBOL, "interval": INTERVAL, "limit": str(1)},
        market_type="spot",
    )
    if isinstance(payload, list) and payload:
        print(f"  [spot]    {SPOT_ENDPOINT}: OK (spot market reachable, NOT used for futures)")
    else:
        print(f"  [spot]    {SPOT_ENDPOINT}: unreachable")


def main() -> int:
    print(f"Binance SOL perpetual connectivity probe")
    print(f"  symbol   : {SYMBOL}")
    print(f"  interval : {INTERVAL}")
    print(f"  limit    : {LIMIT}")
    print()

    print("Probing USDT-margined perpetual futures endpoints (/fapi/v1/klines):")
    any_success = False
    for base_url in FUTURES_ENDPOINTS:
        if probe_futures(base_url):
            any_success = True
            break  # one working endpoint is enough to prove futures access

    print()
    print("Reference probe of spot endpoint (NOT a futures data source):")
    probe_spot()

    print()
    if any_success:
        print("RESULT: SUCCESS - this runtime can download Binance SOLUSDT perpetual candles.")
        return 0
    print("RESULT: FAILURE - no Binance futures endpoint returned valid SOLUSDT candles.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
