#!/usr/bin/env python3
"""Intraday mean-reversion research backtester for SOL-USDT-SWAP (pure stdlib).

Research target
    Validate four intraday mean-reversion methods on OKX SOL-USDT-SWAP
    completed-candle data, with the goal of identifying the most stable
    risk-adjusted configuration.

Data source
    ../DATA/SOL-USDT-SWAP_{5m_103680,15m_34560,30m_17280}_confirmed.csv
    (360 days per timeframe, Beijing time, confirmed candles only,
    produced by the repository's incremental OKX updater).

Methods (each run on 5m, 15m and 30m bars)
    A. Bollinger fade      - close beyond 20-bar / 2-sigma band, exit at mid.
    B. RSI reversion       - RSI(14) <= 30 long / >= 70 short, exit at 50.
    C. VWAP reversion      - per-day anchored VWAP +/- 2 sigma bands,
                             exit at VWAP (naturally intraday).
    D. Regime z-score fade - 60-bar z-score <= -2 / >= +2 only in choppy
                             regimes (efficiency ratio < 0.3), exit at |z| <= 0.5.

Shared intraday framework
    - Signals evaluated on bar close; fills at NEXT bar open (no look-ahead).
    - Hard stop at 2 x ATR(14) from entry, filled intrabar with slippage.
    - Time stop = 4 hours, scaled per timeframe (48/16/8 bars).
    - Forced flat at the last bar of every Beijing day (no overnight risk).
    - Pending entries are cancelled across day boundaries.
    - One position at a time, full-equity 1x notional.

Costs (per side, fee + slippage)
    gross 0.0000 | low 0.0003 | base 0.0007 (OKX VIP0 taker 5bp + 2bp slip)
    high 0.0011.  A round trip at base cost is 14 bp.

Validation
    - 70/30 in-sample / out-of-sample split by day (no parameter tuning;
      all parameters are canonical/textbook values chosen ex ante).
    - Cost sensitivity across the four tiers.
    - Equal-weight portfolio of the four equity curves.
    - Monthly return consistency.

Outputs
    results/<tf>/metrics.json, results/<tf>/trades_<strategy>.csv (base tier),
    results/<tf>/equity_daily.csv (base tier) for each timeframe.

Run
    python3 TRADE/mean_reversion_intraday_backtest.py
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "DATA"
OUT_DIR = Path(__file__).resolve().parent / "results"

SYMBOL = "SOL-USDT-SWAP"

# Same four methods run on three intraday timeframes. Indicator windows stay
# in BARS (canonical values); only intraday-clock quantities are scaled:
# the 4-hour time stop and the 1-hour VWAP daily warm-up.
TIMEFRAMES = {
    "5m": {
        "csv": "SOL-USDT-SWAP_5m_103680_confirmed.csv",
        "bars_per_day": 288,
        "step_ms": 300_000,
        "timestop": 48,   # 48 x 5m = 4h
        "vwap_warmup": 12,  # 12 x 5m = 1h
    },
    "15m": {
        "csv": "SOL-USDT-SWAP_15m_34560_confirmed.csv",
        "bars_per_day": 96,
        "step_ms": 900_000,
        "timestop": 16,   # 16 x 15m = 4h
        "vwap_warmup": 4,   # 4 x 15m = 1h
    },
    "30m": {
        "csv": "SOL-USDT-SWAP_30m_17280_confirmed.csv",
        "bars_per_day": 48,
        "step_ms": 1_800_000,
        "timestop": 8,    # 8 x 30m = 4h
        "vwap_warmup": 2,   # 2 x 30m = 1h
    },
}

ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
WARMUP = 60  # uniform warm-up (in bars) so every strategy trades the same window

COST_TIERS = {
    "gross": 0.0,
    "low": 0.0003,
    "base": 0.0007,
    "high": 0.0011,
}

IS_FRACTION = 0.70


# ---------------------------------------------------------------- data layer


def load_bars(path: Path, step_ms: int):
    times: list[str] = []
    ts: list[int] = []
    o: list[float] = []
    h: list[float] = []
    l: list[float] = []
    c: list[float] = []
    v: list[float] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            times.append(row["open_time_beijing"])
            ts.append(int(row["timestamp_ms"]))
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            l.append(float(row["low"]))
            c.append(float(row["close"]))
            v.append(float(row["vol"]))
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] != step_ms:
            raise RuntimeError(f"candle gap at row {i}: {times[i - 1]} -> {times[i]}")
    return times, ts, o, h, l, c, v


# ------------------------------------------------------------ indicator layer


def rolling_mean_std(values: list[float], win: int):
    n = len(values)
    means: list[float | None] = [None] * n
    stds: list[float | None] = [None] * n
    s = 0.0
    s2 = 0.0
    for i in range(n):
        x = values[i]
        s += x
        s2 += x * x
        if i >= win:
            old = values[i - win]
            s -= old
            s2 -= old * old
        if i >= win - 1:
            m = s / win
            var = max(s2 / win - m * m, 0.0)
            means[i] = m
            stds[i] = math.sqrt(var)
    return means, stds


def wilder_rsi(close: list[float], period: int) -> list[float | None]:
    n = len(close)
    rsi: list[float | None] = [None] * n
    if n <= period:
        return rsi
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = close[i] - close[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        ch = close[i] - close[i - 1]
        g = ch if ch > 0 else 0.0
        ls = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + ls) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rsi[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi


def wilder_atr(h: list[float], l: list[float], c: list[float], period: int) -> list[float | None]:
    n = len(c)
    if n < period:
        return [None] * n
    trs = [h[0] - l[0]]
    for i in range(1, n):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    atr: list[float | None] = [None] * n
    atr[period - 1] = sum(trs[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


def efficiency_ratio(close: list[float], win: int) -> list[float | None]:
    n = len(close)
    er: list[float | None] = [None] * n
    prefix = [0.0] * n
    for i in range(1, n):
        prefix[i] = prefix[i - 1] + abs(close[i] - close[i - 1])
    for i in range(win, n):
        path = prefix[i] - prefix[i - win]
        net = abs(close[i] - close[i - win])
        er[i] = net / path if path > 0 else 0.0
    return er


def day_vwap(
    day_ids: list[str], h: list[float], l: list[float], c: list[float], vol: list[float]
):
    n = len(c)
    vwap: list[float | None] = [None] * n
    sigma: list[float | None] = [None] * n
    day_first: dict[str, int] = {}
    day_last: dict[str, int] = {}
    for i, d in enumerate(day_ids):
        if d not in day_first:
            day_first[d] = i
        day_last[d] = i
    cum_pv = 0.0
    cum_v = 0.0
    cum_var = 0.0
    prev_day = None
    for i in range(n):
        d = day_ids[i]
        if d != prev_day:
            cum_pv = cum_v = cum_var = 0.0
            prev_day = d
        typ = (h[i] + l[i] + c[i]) / 3.0
        cum_pv += typ * vol[i]
        cum_v += vol[i]
        if cum_v > 0:
            vw = cum_pv / cum_v
            cum_var += vol[i] * (typ - vw) ** 2
            vwap[i] = vw
            sigma[i] = math.sqrt(max(cum_var, 0.0) / cum_v)
    return vwap, sigma, day_first, day_last


# ------------------------------------------------------------- signal layer
# Every builder enforces the uniform WARMUP so all strategies share the
# identical backtest window. Signals are evaluated on bar close.


def signals_bollinger(c, warmup=WARMUP, win=20, k=2.0):
    mids, stds = rolling_mean_std(c, win)
    n = len(c)
    le = [False] * n
    se = [False] * n
    lx = [False] * n
    sx = [False] * n
    for i in range(max(warmup, win), n):
        m, s = mids[i], stds[i]
        if m is None or s is None or s == 0:
            continue
        if c[i] <= m - k * s:
            le[i] = True
        elif c[i] >= m + k * s:
            se[i] = True
        if c[i] >= m:
            lx[i] = True
        if c[i] <= m:
            sx[i] = True
    return le, se, lx, sx


def signals_rsi(rsi, warmup=WARMUP, lo=30.0, hi=70.0, mid=50.0):
    n = len(rsi)
    le = [False] * n
    se = [False] * n
    lx = [False] * n
    sx = [False] * n
    for i in range(warmup, n):
        r = rsi[i]
        if r is None:
            continue
        if r <= lo:
            le[i] = True
        elif r >= hi:
            se[i] = True
        if r >= mid:
            lx[i] = True
        if r <= mid:
            sx[i] = True
    return le, se, lx, sx


def signals_vwap(c, vwap, sigma, day_ids, day_first, warmup_in_day=12, k=2.0):
    n = len(c)
    le = [False] * n
    se = [False] * n
    lx = [False] * n
    sx = [False] * n
    for i in range(WARMUP, n):
        vw, s = vwap[i], sigma[i]
        if vw is None or s is None or s == 0:
            continue
        if i - day_first[day_ids[i]] < warmup_in_day:
            continue
        if c[i] <= vw - k * s:
            le[i] = True
        elif c[i] >= vw + k * s:
            se[i] = True
        if c[i] >= vw:
            lx[i] = True
        if c[i] <= vw:
            sx[i] = True
    return le, se, lx, sx


def signals_zscore(c, er, warmup=WARMUP, win=60, k=2.0, exit_z=0.5, er_max=0.30):
    mids, stds = rolling_mean_std(c, win)
    n = len(c)
    le = [False] * n
    se = [False] * n
    lx = [False] * n
    sx = [False] * n
    for i in range(max(warmup, win), n):
        m, s, e = mids[i], stds[i], er[i]
        if m is None or s is None or s == 0 or e is None or e >= er_max:
            continue
        if c[i] <= m - k * s:
            le[i] = True
        elif c[i] >= m + k * s:
            se[i] = True
        if c[i] >= m - exit_z * s:
            lx[i] = True
        if c[i] <= m + exit_z * s:
            sx[i] = True
    return le, se, lx, sx


# ----------------------------------------------------------- simulation core


def simulate(
    times,
    day_ids,
    day_last,
    o,
    h,
    l,
    c,
    atr,
    le,
    se,
    lx,
    sx,
    cost,
    timestop,
    stop_mult=ATR_STOP_MULT,
):
    n = len(c)
    equity = [1.0] * n
    realized = 1.0
    pos = 0
    entry_eff = 0.0
    stop_px = 0.0
    entry_i = -1
    pending = None  # 'L' | 'S' | 'X'
    pending_atr = 0.0
    pending_day = None
    trades: list[dict] = []
    bars_in_pos = 0

    def close_trade(i, px, reason):
        nonlocal realized, pos, pending
        if pos == 1:
            eff = px * (1.0 - cost)
            pnl = eff / entry_eff - 1.0
        else:
            eff = px * (1.0 + cost)
            pnl = 1.0 - eff / entry_eff
        realized *= 1.0 + pnl
        bars_held = i - entry_i + (1 if reason in ("eod", "time") else 0)
        trades.append(
            {
                "side": "long" if pos == 1 else "short",
                "entry_time": times[entry_i],
                "exit_time": times[i],
                "entry_i": entry_i,
                "exit_i": i,
                "entry_px": entry_eff,
                "exit_px": eff,
                "pnl": pnl,
                "bars": bars_held,
                "reason": reason,
            }
        )
        pos = 0
        pending = None

    for i in range(n):
        day = day_ids[i]
        # 1) pending exit -> fill at this bar's open
        if pending == "X" and pos != 0:
            close_trade(i, o[i], "signal")
        # 2) pending entry -> fill at this bar's open
        elif pending in ("L", "S") and pos == 0:
            if day != pending_day or i == day_last[day]:
                pending = None  # never carry signals across a day boundary
            else:
                if pending == "L":
                    entry_eff = o[i] * (1.0 + cost)
                    stop_px = entry_eff - stop_mult * pending_atr
                    pos = 1
                else:
                    entry_eff = o[i] * (1.0 - cost)
                    stop_px = entry_eff + stop_mult * pending_atr
                    pos = -1
                entry_i = i
                pending = None
        # 3) intrabar protective stop
        if pos == 1 and l[i] <= stop_px:
            close_trade(i, stop_px, "stop")
        elif pos == -1 and h[i] >= stop_px:
            close_trade(i, stop_px, "stop")
        # 4) time stop at close
        if pos != 0 and i - entry_i >= timestop:
            close_trade(i, c[i], "time")
        # 5) forced day-end flat at close
        elif pos != 0 and i == day_last[day]:
            close_trade(i, c[i], "eod")
        # 6) signal exit -> schedule at next open
        elif pos == 1 and lx[i]:
            pending = "X"
        elif pos == -1 and sx[i]:
            pending = "X"
        # 7) entry signal at close
        if pos == 0 and pending is None and i < n - 1:
            if le[i]:
                pending = "L"
                pending_atr = atr[i]
                pending_day = day
            elif se[i]:
                pending = "S"
                pending_atr = atr[i]
                pending_day = day
        # 8) mark-to-market equity at close
        if pos == 1:
            equity[i] = realized * (c[i] / entry_eff)
            bars_in_pos += 1
        elif pos == -1:
            equity[i] = realized * (1.0 + (entry_eff - c[i]) / entry_eff)
            bars_in_pos += 1
        else:
            equity[i] = realized
    return equity, trades, bars_in_pos


# --------------------------------------------------------------- metrics


def compute_metrics(equity, trades, bars_in_pos=None, n_days=None, bars_per_year=None):
    if bars_per_year is None:
        bars_per_year = TIMEFRAMES["5m"]["bars_per_day"] * 365
    n = len(equity)
    start = equity[0]
    total = equity[-1] / start - 1.0
    years = (n - 1) / bars_per_year
    if years > 0 and equity[-1] > 0:
        ann = (equity[-1] / start) ** (1.0 / years) - 1.0
    elif equity[-1] <= 0:
        ann = -1.0
    else:
        ann = 0.0

    rets = [equity[i] / equity[i - 1] - 1.0 for i in range(1, n)]
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    sd = math.sqrt(var)
    sharpe = mean_r / sd * math.sqrt(bars_per_year) if sd > 0 else 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets)) if rets else 0.0
    sortino = mean_r / downside * math.sqrt(bars_per_year) if downside > 0 else None

    peak = equity[0]
    mdd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = 1.0 - e / peak
        if dd > mdd:
            mdd = dd
    calmar = ann / mdd if mdd > 0 else None

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    n_tr = len(trades)
    avg_trade = sum(t["pnl"] for t in trades) / n_tr if n_tr else 0.0
    stats = {
        "trades": n_tr,
        "win_rate": len(wins) / n_tr if n_tr else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_trade": avg_trade,
        "breakeven_per_side": avg_trade / 2.0 if n_tr else None,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "best_trade": max((t["pnl"] for t in trades), default=0.0),
        "worst_trade": min((t["pnl"] for t in trades), default=0.0),
        "avg_bars_held": sum(t["bars"] for t in trades) / n_tr if n_tr else 0.0,
        "trades_per_day": n_tr / n_days if n_days else 0.0,
        "exit_reasons": {
            r: sum(1 for t in trades if t["reason"] == r)
            for r in ("signal", "stop", "time", "eod")
        },
    }
    m = {
        "total_return": total,
        "ann_return": ann,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "calmar": calmar,
        "ann_vol": sd * math.sqrt(bars_per_year),
        "exposure": (bars_in_pos / (n - 1)) if bars_in_pos is not None else None,
        **stats,
    }
    return m


def monthly_returns(equity, day_ids, day_last):
    days = sorted(day_last)
    daily = []
    for d in days:
        daily.append((d, equity[day_last[d]]))
    months: dict[str, float] = {}
    prev_eq = daily[0][1]
    for d, eq in daily:
        r = eq / prev_eq - 1.0
        prev_eq = eq
        key = d[:7]
        months[key] = (1.0 + months.get(key, 0.0)) * (1.0 + r) - 1.0
    return [(k, v) for k, v in sorted(months.items())]


def is_oos_split(day_first, day_ids):
    days = sorted(day_first)
    split_idx = int(len(days) * IS_FRACTION)
    boundary = day_first[days[split_idx]]
    return boundary, days[split_idx]


def slice_metrics(equity, trades, lo, hi, bars_per_year, renorm_base=None):
    seg = equity[lo:hi]
    if renorm_base is not None:
        base = renorm_base
        seg = [x / base for x in seg]
    seg_trades = [t for t in trades if lo <= t["entry_i"] < hi]
    return compute_metrics(seg, seg_trades, bars_per_year=bars_per_year)


# ------------------------------------------------------------------- main


def run_timeframe(tf: str, cfg: dict) -> None:
    bpy = cfg["bars_per_day"] * 365
    out = OUT_DIR / tf
    out.mkdir(parents=True, exist_ok=True)

    times, ts, o, h, l, c, v = load_bars(DATA_DIR / cfg["csv"], cfg["step_ms"])
    day_ids = [t[:10] for t in times]
    vwap, sigma, day_first, day_last = day_vwap(day_ids, h, l, c, v)
    rsi = wilder_rsi(c, 14)
    atr = wilder_atr(h, l, c, ATR_PERIOD)
    er = efficiency_ratio(c, 60)
    boundary, oos_first_day = is_oos_split(day_first, day_ids)
    n_days = len(day_first)
    print(f"\n########## {tf}: bars={len(c)}  days={n_days}  "
          f"{times[0]} -> {times[-1]} (Beijing)")
    print(f"IS/OOS boundary at bar {boundary} ({times[boundary]}), OOS starts {oos_first_day}")

    strategies = {
        "A_bollinger": {
            "signals": signals_bollinger(c),
            "timestop": cfg["timestop"],
        },
        "B_rsi": {
            "signals": signals_rsi(rsi),
            "timestop": cfg["timestop"],
        },
        "C_vwap": {
            "signals": signals_vwap(c, vwap, sigma, day_ids, day_first,
                                    warmup_in_day=cfg["vwap_warmup"]),
            "timestop": cfg["timestop"],
        },
        "D_zscore": {
            "signals": signals_zscore(c, er),
            "timestop": cfg["timestop"],
        },
    }

    results = {
        "meta": {
            "symbol": SYMBOL,
            "csv": cfg["csv"],
            "bars_per_day": cfg["bars_per_day"],
            "bars_per_year": bpy,
            "timestop_bars": cfg["timestop"],
            "vwap_warmup_bars": cfg["vwap_warmup"],
            "bars": len(c),
            "days": n_days,
            "start_beijing": times[0],
            "end_beijing": times[-1],
            "is_fraction": IS_FRACTION,
            "oos_first_day": oos_first_day,
            "boundary_bar": boundary,
            "cost_tiers_per_side": COST_TIERS,
            "atr_stop_mult": ATR_STOP_MULT,
            "warmup_bars": WARMUP,
        },
        "strategies": {},
        "portfolio": {},
        "benchmark": {},
    }

    equities_by_tier: dict[str, dict[str, list[float]]] = {}
    trades_by_tier: dict[str, dict[str, list[dict]]] = {}

    for name, scfg in strategies.items():
        le, se, lx, sx = scfg["signals"]
        entry = results["strategies"].setdefault(name, {})
        for tier, cost in COST_TIERS.items():
            equity, trades, bars_in_pos = simulate(
                times, day_ids, day_last, o, h, l, c, atr,
                le, se, lx, sx, cost, scfg["timestop"],
            )
            m = compute_metrics(equity, trades, bars_in_pos, n_days, bpy)
            entry[tier] = m
            equities_by_tier.setdefault(tier, {})[name] = equity
            trades_by_tier.setdefault(tier, {})[name] = trades
            if tier == "base" and trades:
                with (out / f"trades_{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(trades[0].keys()))
                    w.writeheader()
                    w.writerows(trades)
        for tier in ("gross", "base"):
            eq = equities_by_tier[tier][name]
            tr = trades_by_tier[tier][name]
            entry[f"is_{tier}"] = slice_metrics(eq, tr, 0, boundary + 1, bpy)
            entry[f"oos_{tier}"] = slice_metrics(
                eq, tr, boundary, len(c), bpy, renorm_base=eq[boundary]
            )
        print(f"{name}: done (base tier sharpe={entry['base']['sharpe']:.2f}, "
              f"total={entry['base']['total_return'] * 100:.1f}%)")

    # equal-weight portfolio (average of normalized equity curves)
    for tier in ("gross", "base"):
        eqs = equities_by_tier[tier]
        names = sorted(eqs)
        port = [sum(eqs[nm][i] for nm in names) / len(names) for i in range(len(c))]
        entry = results["portfolio"].setdefault(tier, {})
        # component trades are not merged; trade stats live on the strategies.
        entry["full"] = compute_metrics(port, [], None, n_days, bpy)
        entry["monthly"] = monthly_returns(port, day_ids, day_last)
        entry["is"] = slice_metrics(port, [], 0, boundary + 1, bpy)
        entry["oos"] = slice_metrics(port, [], boundary, len(c), bpy, renorm_base=port[boundary])
    for name, eq in equities_by_tier["base"].items():
        results["strategies"][name]["monthly"] = monthly_returns(eq, day_ids, day_last)

    # buy & hold benchmark (reference only: it is NOT intraday)
    bh = [c[i] / c[0] for i in range(len(c))]
    results["benchmark"]["buy_hold"] = compute_metrics(bh, [], None, n_days, bpy)

    with (out / "metrics.json").open("w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    days = sorted(day_last)
    with (out / "equity_daily.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "A_bollinger", "B_rsi", "C_vwap", "D_zscore", "portfolio", "buy_hold"])
        base_eqs = equities_by_tier["base"]
        nm_list = sorted(base_eqs)
        for d in days:
            i = day_last[d]
            row = [base_eqs[nm][i] for nm in nm_list]
            w.writerow([d] + row + [sum(row) / len(row), bh[i]])

    # ---- console summary ----
    def fmt(x, spec):
        return "-" if x is None else format(x, spec)

    print(f"\n=== {tf} full period, base cost tier (7 bp per side) ===")
    print(f"{'strategy':<14}{'total%':>9}{'ann%':>8}{'sharpe':>8}{'maxDD%':>8}"
          f"{'trades':>8}{'win%':>7}{'PF':>7}{'breakeven':>11}")
    rows = [(nm, results["strategies"][nm]["base"]) for nm in sorted(results["strategies"])]
    rows.append(("portfolio", results["portfolio"]["base"]["full"]))
    rows.append(("buy_hold", results["benchmark"]["buy_hold"]))
    for nm, m in rows:
        be = m["breakeven_per_side"]
        be_str = "-" if be is None else f"{be * 10000:.1f}bp"
        print(
            f"{nm:<14}{m['total_return']*100:>9.1f}{m['ann_return']*100:>8.1f}"
            f"{m['sharpe']:>8.2f}{m['max_drawdown']*100:>8.1f}{m['trades']:>8}"
            f"{m['win_rate']*100:>7.1f}{fmt(m['profit_factor'], '.2f'):>7}{be_str:>11}"
        )
    print(f"\n=== {tf} cost sensitivity (total return %) ===")
    print(f"{'strategy':<14}" + "".join(f"{t:>10}" for t in COST_TIERS))
    for nm in sorted(results["strategies"]):
        row = f"{nm:<14}"
        for t in COST_TIERS:
            row += f"{results['strategies'][nm][t]['total_return']*100:>10.1f}"
        print(row)
    print(f"\n=== {tf} IS / OOS sharpe (gross | base) ===")
    print(f"{'strategy':<14}{'IS(g)':>8}{'OOS(g)':>8}{'IS(b)':>8}{'OOS(b)':>8}")
    for nm in sorted(results["strategies"]):
        s = results["strategies"][nm]
        print(
            f"{nm:<14}{s['is_gross']['sharpe']:>8.2f}{s['oos_gross']['sharpe']:>8.2f}"
            f"{s['is_base']['sharpe']:>8.2f}{s['oos_base']['sharpe']:>8.2f}"
        )
    print(f"\nresults written to {out}")


def main() -> int:
    for tf, cfg in TIMEFRAMES.items():
        run_timeframe(tf, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
