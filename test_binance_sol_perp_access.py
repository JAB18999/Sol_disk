#!/usr/bin/env python3
"""Probe whether the runtime (GitHub Actions runner or local machine) can
reach the Binance USDT-margined perpetual (futures) market data API and
download SOL-USDT candles.

This is a connectivity / data-source sanity test, not a production downloader.
It only fetches a small number of confirmed candles and prints a verifiable
summary: the exact endpoint used, the HTTP status, the returned symbol, the
candle count, the first/last timestamps, and a sample OHLC row.

Binance official endpoints probed
    USDT-M perpetual (futures) - /fapi/v1/klines (the real data source):
        primary : https://fapi.binance.com
        backups : https://fapi1.binance.com, fapi2, fapi3, fapi4
    Spot REST base endpoints - /api/v3/klines (reachability reference only,
    these do NOT supply perpetual data):
        https://api.binance.com
        https://api-gcp.binance.com
        https://api1.binance.com, api2, api3, api4
        https://data-api.binance.vision   (official public market-data domain)
        https://api.binance.us            (Binance.US entity, spot only)

Symbol mapping
    SOL-USDT-SWAP in OKX notation  ->  SOLUSDT on Binance (USDT-margined
    perpetual contract under the /fapi market).

Exit codes
    0  at least one futures endpoint returned valid SOLUSDT candles
    1  all futures probes failed

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

# USDT-margined perpetual futures endpoints: primary + official backups.
# These are the endpoints that actually serve /fapi/v1/klines for SOLUSDT.
FUTURES_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
]

# Spot REST base endpoints (official primary + backups + public data domain).
# Probed only to gauge overall Binance reachability; they never serve
# perpetual candles and are NOT used as a fallback for futures data.
SPOT_ENDPOINTS = [
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://data-api.binance.vision",
    "https://api.binance.us",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; binance-sol-perp-probe/1.0)",
    "Accept": "application/json",
}
MAX_RETRIES = 3
TIMEOUT_SECONDS = 20


def beijing_time(timestamp_ms: int) -> str:
    instant = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return instant.astimezone(BEIJING_TIMEZONE).isoformat()


def fetch_json(base_url: str, params: dict[str, str], market_type: str):
    """Fetch JSON from one Binance endpoint.

    Returns (payload, status_note) where payload is the parsed JSON body
    (or None on failure) and status_note is a short human-readable reason.
    """
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    note = "no response"
    for attempt in range(MAX_RETRIES):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw), f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            note = f"HTTP {exc.code}"
            if "restricted location" in body:
                note += " (geo-blocked: restricted location)"
            print(f"  [{market_type}] {base_url}: {note}")
            # 4xx (incl. 451 geo-block) / 429 are not worth retrying.
            if exc.code in {400, 401, 403, 404, 429, 451}:
                return None, note
        except json.JSONDecodeError:
            note = "JSONDecodeError (non-JSON response, likely a block page)"
            print(f"  [{market_type}] {base_url}: {note}")
            return None, note
        except (urllib.error.URLError, TimeoutError) as exc:
            note = f"{type(exc).__name__}: {exc}"
            print(f"  [{market_type}] {base_url}: {note}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(random.uniform(0.5, 2.0))
    return None, note


def probe_futures(base_url: str) -> bool:
    """Try to fetch SOLUSDT perpetual candles from one futures endpoint."""
    payload, note = fetch_json(
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
    print(f"  [futures] {base_url}: OK ({note})")
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


def probe_spot(base_url: str) -> bool:
    """Probe a spot base endpoint as a reachability reference only."""
    payload, note = fetch_json(
        base_url,
        {"symbol": SYMBOL, "interval": INTERVAL, "limit": "1"},
        market_type="spot",
    )
    ok = isinstance(payload, list) and bool(payload)
    if ok:
        print(f"  [spot]    {base_url}: OK ({note}) (NOT used for futures)")
    return ok


def main() -> int:
    print("Binance SOL perpetual connectivity probe")
    print(f"  symbol   : {SYMBOL}")
    print(f"  interval : {INTERVAL}")
    print(f"  limit    : {LIMIT}")
    print()

    print("Futures endpoints (USDT-M perpetual, /fapi/v1/klines):")
    futures_ok: list[str] = []
    futures_results: list[tuple[str, str]] = []
    for base_url in FUTURES_ENDPOINTS:
        if probe_futures(base_url):
            futures_ok.append(base_url)
            futures_results.append((base_url, "SUCCESS"))
        else:
            futures_results.append((base_url, "FAIL"))
            print(f"  [futures] {base_url}: FAIL")

    print()
    print("Spot base endpoints (reachability reference, /api/v3/klines):")
    spot_results: list[tuple[str, str]] = []
    for base_url in SPOT_ENDPOINTS:
        ok = probe_spot(base_url)
        spot_results.append((base_url, "OK" if ok else "FAIL"))
        if not ok:
            print(f"  [spot]    {base_url}: FAIL")

    print()
    print("Summary:")
    print(f"  futures endpoints reachable: {len(futures_ok)}/{len(FUTURES_ENDPOINTS)}")
    for base_url, status in futures_results:
        print(f"      [{status:7}] {base_url}")
    print(f"  spot endpoints reachable   : "
          f"{sum(1 for _, s in spot_results if s == 'OK')}/{len(SPOT_ENDPOINTS)}")
    for base_url, status in spot_results:
        print(f"      [{status:4}] {base_url}")

    print()
    if futures_ok:
        print("RESULT: SUCCESS - this runtime can download Binance SOLUSDT perpetual "
              f"candles from: {', '.join(futures_ok)}")
        return 0
    print("RESULT: FAILURE - no Binance futures endpoint returned valid SOLUSDT candles.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
