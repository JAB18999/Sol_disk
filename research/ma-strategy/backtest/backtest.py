"""MA 双均线策略回测引擎。

严格遵循预注册 research/ma-strategy/preregistration-v1.md：
- 双向趋势跟踪：金叉开多、死叉开空，反向信号平仓并反手
- 基于收盘价计算快慢 SMA；信号于 K 线收盘后确认、下一根开盘执行（防 lookahead）
- 单笔名义价值 = 权益 × 0.8；固定止损 3%；熔断仅打标记 breach_20pct，不干预执行
- 无资金费率、无滑点；样本末持仓按最后收盘价 end_of_sample 结算
- 评价指标锁定：年化夏普比率、最大回撤 MDD（基于收盘价 equity 峰谷回撤）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# 允许直接运行此脚本时导入同目录的 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402


def _parse_beijing(ts) -> pd.Timestamp:
    """把 open_time_beijing 解析为 Asia/Shanghai 时区的 Timestamp，兼容带偏移与裸时间。"""
    parsed = pd.to_datetime(ts)
    if parsed.tzinfo is None:
        return parsed.tz_localize(config.TIMEZONE)
    return parsed.tz_convert(config.TIMEZONE)


def load_grain(data_dir: str, grain: Dict) -> Tuple[pd.DataFrame, Dict]:
    """读取指定粒度的 CSV，按 open_time_beijing 排序，返回 (df, meta)。

    期望 CSV 列包含：open_time_beijing, open, high, low, close[, volume]。
    """
    path = os.path.join(data_dir, grain["filename"])
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "open_time_beijing" not in df.columns:
        raise ValueError(f"{path} 缺少 open_time_beijing 列")
    df["open_time_beijing"] = df["open_time_beijing"].apply(_parse_beijing)
    df = df.sort_values("open_time_beijing").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    meta = {
        "grain": grain["name"],
        "filename": grain["filename"],
        "minutes": grain["minutes"],
        "bars_per_year": grain["bars_per_year"],
        "rows": int(len(df)),
        "start": str(df["open_time_beijing"].iloc[0]) if len(df) else None,
        "end": str(df["open_time_beijing"].iloc[-1]) if len(df) else None,
    }
    return df, meta


def compute_metrics(equity: np.ndarray, bars_per_year: int, breach_threshold: float) -> Dict:
    """基于收盘价 equity 序列计算年化夏普与 MDD。

    返回 {sharpe, sharpe_note, mdd, breach_20pct}。
    """
    equity = np.asarray(equity, dtype=float)
    if len(equity) < 2:
        return {"sharpe": 0.0, "sharpe_note": "insufficient_data", "mdd": 0.0, "breach_20pct": False}

    returns = np.diff(equity) / equity[:-1]
    returns = returns[~np.isnan(returns)]
    if len(returns) == 0:
        return {"sharpe": 0.0, "sharpe_note": "no_returns", "mdd": 0.0, "breach_20pct": False}

    mean_ret = float(np.mean(returns))
    if len(returns) > 1:
        std = float(np.std(returns, ddof=1))
    else:
        std = 0.0
    if std <= 0:
        sharpe = 0.0
        sharpe_note = "zero_std"
    else:
        sharpe = float(mean_ret / std * np.sqrt(float(bars_per_year)))
        sharpe_note = "ok"

    # MDD：基于 equity 峰谷回撤（收盘价序列）
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    mdd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
    breach_20pct = bool(mdd > breach_threshold)

    return {
        "sharpe": round(sharpe, 6),
        "sharpe_note": sharpe_note,
        "mdd": round(mdd, 6),
        "breach_20pct": breach_20pct,
    }


def _empty_row(grain: Dict, fast: int, slow: int, split_name: str) -> Dict:
    return {
        "grain": grain["name"],
        "fast": fast,
        "slow": slow,
        "split": split_name,
        "sharpe": 0.0,
        "sharpe_note": "no_data",
        "mdd": 0.0,
        "breach_20pct": False,
        "total_trades": 0,
        "total_pnl": 0.0,
        "final_equity": config.INITIAL_EQUITY,
    }


def _pnl(position: int, entry_price: float, exit_price: float, qty: float) -> float:
    if position == 1:
        return (exit_price - entry_price) * qty
    return (entry_price - exit_price) * qty


def run_backtest(full: pd.DataFrame, grain: Dict, fast: int, slow: int, split: Dict) -> Tuple[Dict, List[Dict]]:
    """单组参数在一个样本区间上的回测。

    返回 (row, trades)。row 为单行汇总指标；trades 为每笔完整交易记录。
    """
    name = split["name"]
    start = pd.Timestamp(split["start"]).tz_localize(config.TIMEZONE)
    end = pd.Timestamp(split["end"]).tz_localize(config.TIMEZONE)

    # 切片样本区间（两端闭区间，end 为 23:59）
    mask = (full["open_time_beijing"] >= start) & (full["open_time_beijing"] <= end)
    bars = full.loc[mask].copy()
    if len(bars) == 0:
        return _empty_row(grain, fast, slow, name), []

    bars = bars.reset_index(drop=True)
    close = bars["close"].to_numpy(dtype=float)
    openp = bars["open"].to_numpy(dtype=float)
    times = bars["open_time_beijing"]

    bars_per_year = grain["bars_per_year"]

    # 基于收盘价的 SMA（前 slow-1 根为 NaN，不产生信号）
    fast_ma = pd.Series(close).rolling(window=fast, min_periods=fast).mean().to_numpy()
    slow_ma = pd.Series(close).rolling(window=slow, min_periods=slow).mean().to_numpy()

    equity = config.INITIAL_EQUITY
    position = 0          # 1: long, -1: short, 0: flat
    entry_price = 0.0
    entry_time = None
    qty = 0.0

    trades: List[Dict] = []
    equity_curve: List[float] = []

    n = len(bars)
    pending = None  # 上一根收盘后规划的、在本根开盘执行的动作

    for i in range(n):
        cur_open = openp[i]
        cur_close = close[i]
        cur_time = times.iloc[i]

        # --- A. 执行上一根收盘后规划的本根开盘动作 ---
        if pending is not None:
            action = pending["action"]
            sig = pending["signal"]
            px = pending["price"]
            ptime = pending["time"]
            if action == "close" and position != 0:
                pnl_val = _pnl(position, entry_price, px, qty)
                equity += pnl_val
                trades.append({
                    "grain": grain["name"],
                    "fast": fast,
                    "slow": slow,
                    "split": name,
                    "side": "long" if position == 1 else "short",
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(px, 6),
                    "qty": round(qty, 6),
                    "pnl": round(pnl_val, 6),
                    "exit_reason": "stop_loss",
                    "entry_time": str(entry_time),
                    "exit_time": str(ptime),
                })
                position = 0
                qty = 0.0
                entry_price = 0.0
                entry_time = None
            elif action == "close_reverse" and position != 0:
                # 平旧仓
                pnl_val = _pnl(position, entry_price, px, qty)
                equity += pnl_val
                trades.append({
                    "grain": grain["name"],
                    "fast": fast,
                    "slow": slow,
                    "split": name,
                    "side": "long" if position == 1 else "short",
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(px, 6),
                    "qty": round(qty, 6),
                    "pnl": round(pnl_val, 6),
                    "exit_reason": "reverse_signal",
                    "entry_time": str(entry_time),
                    "exit_time": str(ptime),
                })
                # 立即反手开新仓（下一根开盘价执行）
                position = sig
                entry_price = px
                entry_time = ptime
                qty = equity * config.POSITION_FRACTION / px if px != 0 else 0.0
            elif action == "open" and position == 0:
                # 下一根开盘价执行开仓
                position = sig
                entry_price = px
                entry_time = ptime
                qty = equity * config.POSITION_FRACTION / px if px != 0 else 0.0
            # action == "hold" / "idle"：无操作
            pending = None

        # --- B. 记录本根收盘时的总 equity（含未实现盈亏） ---
        if position != 0 and qty > 0:
            if position == 1:
                unrealized = (cur_close - entry_price) * qty
            else:
                unrealized = (entry_price - cur_close) * qty
        else:
            unrealized = 0.0
        equity_curve.append(equity + unrealized)

        # --- C. 样本末：若仍持仓，按最后收盘价 end_of_sample 平仓 ---
        if i == n - 1 and position != 0:
            exit_price = cur_close
            pnl_val = _pnl(position, entry_price, exit_price, qty)
            equity += pnl_val
            trades.append({
                "grain": grain["name"],
                "fast": fast,
                "slow": slow,
                "split": name,
                "side": "long" if position == 1 else "short",
                "entry_price": round(entry_price, 6),
                "exit_price": round(exit_price, 6),
                "qty": round(qty, 6),
                "pnl": round(pnl_val, 6),
                "exit_reason": "end_of_sample",
                "entry_time": str(entry_time),
                "exit_time": str(cur_time),
            })
            position = 0
            qty = 0.0
            entry_price = 0.0
            entry_time = None
            break

        # --- D. 基于本根收盘价判断止损/信号，规划下一根 (i+1) 开盘动作 ---
        if i + 1 < n:
            next_open = openp[i + 1]
            next_time = times.iloc[i + 1]

            # 信号判定（需两根均有有效 MA）
            signal = 0
            if (i >= 1
                    and not np.isnan(fast_ma[i]) and not np.isnan(slow_ma[i])
                    and not np.isnan(fast_ma[i - 1]) and not np.isnan(slow_ma[i - 1])):
                if fast_ma[i - 1] <= slow_ma[i - 1] and fast_ma[i] > slow_ma[i]:
                    signal = 1   # 金叉
                elif fast_ma[i - 1] >= slow_ma[i - 1] and fast_ma[i] < slow_ma[i]:
                    signal = -1  # 死叉

            plan_action = None
            plan_signal = 0
            if position != 0:
                stopped = False
                if position == 1 and cur_close <= entry_price * (1.0 - config.STOP_LOSS_PCT):
                    stopped = True
                if position == -1 and cur_close >= entry_price * (1.0 + config.STOP_LOSS_PCT):
                    stopped = True
                if stopped:
                    plan_action = "close"
                elif signal != 0 and signal != position:
                    plan_action = "close_reverse"
                    plan_signal = signal
                else:
                    plan_action = "hold"
            else:
                if signal != 0:
                    plan_action = "open"
                    plan_signal = signal
                else:
                    plan_action = "idle"

            pending = {
                "action": plan_action,
                "signal": plan_signal,
                "price": next_open,
                "time": next_time,
            }

    # --- 汇总指标 ---
    equity_arr = np.asarray(equity_curve, dtype=float)
    metrics = compute_metrics(equity_arr, bars_per_year, config.BREACH_MDD_THRESHOLD)
    total_pnl = sum(t["pnl"] for t in trades)
    row = {
        "grain": grain["name"],
        "fast": fast,
        "slow": slow,
        "split": name,
        "sharpe": metrics["sharpe"],
        "sharpe_note": metrics["sharpe_note"],
        "mdd": metrics["mdd"],
        "breach_20pct": metrics["breach_20pct"],
        "total_trades": len(trades),
        "total_pnl": round(total_pnl, 6),
        "final_equity": round(equity, 6),
    }
    return row, trades


def run_stage(stage: str, grain_data: Dict[str, pd.DataFrame], param_sets: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """运行一个阶段（train/validate/test），返回 (rows, trade_rows)。"""
    split = config.split_by_name(stage)
    rows: List[Dict] = []
    trade_rows: List[Dict] = []
    for p in param_sets:
        grain_name = p["grain"]
        grain = config.grain_by_name(grain_name)
        full = grain_data[grain_name]
        row, trades = run_backtest(full, grain, p["fast"], p["slow"], split)
        rows.append(row)
        trade_rows.extend(trades)
    return rows, trade_rows


def rule_b_decisions(train_rows: List[Dict]) -> List[Dict]:
    """规则 B：粒度级过滤。

    对每个粒度的 4 组参数：若 4 组全部满足 (a) 夏普 <= 0 或 (b) MDD > 20%，
    则该粒度“无可用参数”。否则从不满足 (a) 且不满足 (b) 的组中
    （即 夏普 > 0 且 MDD <= 20%）按夏普最高选 1 组进入验证集。
    """
    by_grain: Dict[str, List[Dict]] = {}
    for r in train_rows:
        by_grain.setdefault(r["grain"], []).append(r)

    decisions: List[Dict] = []
    for grain_name, rows in by_grain.items():
        eligible = [
            r for r in rows
            if r["sharpe"] > config.RULE_B_SHARPE_MIN
            and r["mdd"] <= config.RULE_B_MDD_MAX
        ]
        all_bad = all(
            (r["sharpe"] <= config.RULE_B_SHARPE_MIN) or (r["mdd"] > config.RULE_B_MDD_MAX)
            for r in rows
        )
        if all_bad or len(eligible) == 0:
            decisions.append({"grain": grain_name, "selected": None, "reason": "no_eligible"})
        else:
            best = max(eligible, key=lambda r: r["sharpe"])
            decisions.append({"grain": grain_name, "selected": best, "reason": "ok"})
    return decisions


def selected_param_sets(decisions: List[Dict]) -> List[Dict]:
    """从规则 B 决策中选出进入验证集的参数集合。"""
    sets: List[Dict] = []
    for d in decisions:
        if d["selected"] is not None:
            sets.append({
                "grain": d["selected"]["grain"],
                "fast": d["selected"]["fast"],
                "slow": d["selected"]["slow"],
            })
    return sets


def write_csv(rows: List[Dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_summary(decisions: List[Dict], selected: List[Dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "decisions": decisions,
        "selected_param_sets": selected,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_all_grain(data_dir: str) -> Dict[str, pd.DataFrame]:
    grain_data: Dict[str, pd.DataFrame] = {}
    for g in config.GRAIN:
        df, _ = load_grain(data_dir, g)
        grain_data[g["name"]] = df
    return grain_data


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="MA 双均线策略回测")
    parser.add_argument("--mode", choices=["train", "validate", "test", "all"], default="all")
    parser.add_argument("--data-dir", default=config.DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    grain_data = _load_all_grain(data_dir)

    os.makedirs(config.ARTIFACT_DIR, exist_ok=True)

    mode = args.mode

    # 训练集：全量参数（经规则 A 过滤后）+ 规则 B 决策，产出进入验证集的参数
    if mode in ("train", "all"):
        param_sets = config.apply_rule_a(config.PARAM_GRID)
        train_rows, train_trades = run_stage("train", grain_data, param_sets)
        write_csv(train_rows, os.path.join(config.ARTIFACT_DIR, "train_results.csv"))
        write_csv(train_trades, os.path.join(config.ARTIFACT_DIR, "train_trades.csv"))
        decisions = rule_b_decisions(train_rows)
        selected = selected_param_sets(decisions)
        write_summary(decisions, selected, os.path.join(config.ARTIFACT_DIR, "rule_b_summary.json"))
        print(f"[train] 参数组数={len(param_sets)}, 进入验证集粒度数={len(selected)}")
        for d in decisions:
            print(f"  {d['grain']}: {d['reason']}" +
                  (f" selected=fast={d['selected']['fast']} slow={d['selected']['slow']} "
                   f"sharpe={d['selected']['sharpe']} mdd={d['selected']['mdd']}" if d["selected"] else ""))
        selected_params_path = os.path.join(config.ARTIFACT_DIR, "selected_params.json")
        with open(selected_params_path, "w", encoding="utf-8") as f:
            json.dump(selected, f, ensure_ascii=False, indent=2)

    # 验证集 / 测试集：使用选出的参数
    selected: List[Dict] = []
    if mode in ("validate", "test", "all"):
        selected_params_path = os.path.join(config.ARTIFACT_DIR, "selected_params.json")
        if os.path.exists(selected_params_path):
            with open(selected_params_path, "r", encoding="utf-8") as f:
                selected = json.load(f)
        else:
            print(f"[WARN] 未找到 {selected_params_path}，{mode} 阶段将跳过")
            selected = []

    if mode in ("validate", "all") and selected:
        val_rows, val_trades = run_stage("validate", grain_data, selected)
        write_csv(val_rows, os.path.join(config.ARTIFACT_DIR, "validate_results.csv"))
        write_csv(val_trades, os.path.join(config.ARTIFACT_DIR, "validate_trades.csv"))
        print(f"[validate] 参数组数={len(selected)}")

    if mode in ("test", "all") and selected:
        test_rows, test_trades = run_stage("test", grain_data, selected)
        write_csv(test_rows, os.path.join(config.ARTIFACT_DIR, "test_results.csv"))
        write_csv(test_trades, os.path.join(config.ARTIFACT_DIR, "test_trades.csv"))
        print(f"[test] 参数组数={len(selected)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())