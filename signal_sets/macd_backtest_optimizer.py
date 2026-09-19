#!/usr/bin/env python3
"""MACD 信号集的事件驱动回测与参数优化器。

本脚本实现 `macd_signal_set_minimal.md` 中的十类核心信号，并参考
`交易与仓位管理规则.md` 的仓位、减仓、反手和 48 小时时间退出规则。

输出文件默认均写入本脚本所在的 signal_sets/ 目录：
  - macd_parameter_optimization_results.csv    全部参数组合结果
  - macd_backtest_report.md                    优化方法和最优参数报告
  - macd_best_trades_<周期>.csv                各周期最优参数的样本外成交记录
  - macd_best_equity_<周期>.csv                各周期最优参数的样本外权益曲线

设计原则：
  * 在已收盘 K 线的收盘价计算信号，并于下一根 K 线开盘执行，避免未来函数；
  * 固定止盈按当根 K 线 OHLC 检查；
  * CSV 的可读时间使用北京时间（Asia/Shanghai，UTC+08:00）；
  * timestamp_ms 是不带时区的 Unix Epoch 毫秒键，不能加 8 小时；
  * 本程序为研究回测，不含资金费、强平、盘口深度和真实限价单未成交风险。

示例：
  python3 signal_sets/macd_backtest_optimizer.py
  python3 signal_sets/macd_backtest_optimizer.py --timeframes 15m,1H
  python3 signal_sets/macd_backtest_optimizer.py --top-n 10
  python3 signal_sets/macd_backtest_optimizer.py --timeframes 15m \
      --fast-periods 12 --slow-periods 26 --signal-periods 9 \
      --take-profit-pcts 0.02 --histogram-expansion-bars 2
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import statistics
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INSTRUMENT = "SOL-USDT-SWAP"
DATA_COUNT = 8640
BEIJING = ZoneInfo("Asia/Shanghai")
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
TIMEFRAMES_MS = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "2H": 2 * 60 * 60 * 1000,
}


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    time_beijing: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class StrategyParams:
    fast_period: int
    slow_period: int
    signal_period: int
    take_profit_pct: float
    histogram_expansion_bars: int
    divergence_lookback: int
    pivot_window: int


@dataclass(frozen=True)
class BacktestConfig:
    unit_notional_usdt: float
    initial_capital_usdt: float
    fee_bps: float
    slippage_bps: float
    time_stop_hours: float
    divergence_confirmation_bars: int
    train_ratio: float
    min_trades_for_ranking: int


@dataclass
class Indicators:
    dif: list[float]
    dea: list[float]
    histogram: list[float]
    golden_cross: list[bool]
    dead_cross: list[bool]
    zero_up: list[bool]
    zero_down: list[bool]
    bullish_divergence: list[bool]
    bearish_divergence: list[bool]


@dataclass
class Action:
    kind: str  # open_or_add, flip, reduce, time_stop
    side: str  # long or short
    reason: str
    units: int = 1


@dataclass
class Lot:
    side: str
    quantity_sol: float
    entry_price: float
    entry_fee: float
    entry_timestamp_ms: int
    entry_time_beijing: str
    entry_bar: int
    entry_reason: str


@dataclass
class Metrics:
    initial_capital_usdt: float
    final_equity_usdt: float
    net_pnl_usdt: float
    net_return_pct: float
    max_drawdown_pct: float
    annualized_sharpe: float
    closed_lots: int
    win_rate_pct: float
    profit_factor: float | None
    total_fees_usdt: float
    skipped_entry_actions: int
    forced_end_closures: int


@dataclass
class BacktestRun:
    metrics: Metrics
    events: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]


@dataclass
class OptimizationRow:
    timeframe: str
    fast_period: int
    slow_period: int
    signal_period: int
    take_profit_pct: float
    histogram_expansion_bars: int
    divergence_lookback: int
    pivot_window: int
    train_bars: int
    test_bars: int
    train_score: float
    train_return_pct: float
    train_max_drawdown_pct: float
    train_sharpe: float
    train_closed_lots: int
    train_win_rate_pct: float
    train_profit_factor: float | None
    test_return_pct: float
    test_max_drawdown_pct: float
    test_sharpe: float
    test_closed_lots: int
    test_win_rate_pct: float
    test_profit_factor: float | None
    test_net_pnl_usdt: float
    is_best_for_timeframe: bool = False


class DataValidationError(RuntimeError):
    """Raised when candle data cannot be used for a look-ahead-safe backtest."""


def beijing_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(BEIJING).isoformat()


def parse_int_list(value: str) -> list[int]:
    values = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("参数列表不能为空")
    return sorted(set(values))


def parse_float_list(value: str) -> list[float]:
    values = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("参数列表不能为空")
    return sorted(set(values))


def parse_timeframes(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in items if item not in TIMEFRAMES_MS]
    if unknown:
        raise argparse.ArgumentTypeError(f"不支持的周期：{', '.join(unknown)}；可选 {', '.join(TIMEFRAMES_MS)}")
    if not items:
        raise argparse.ArgumentTypeError("至少选择一个周期")
    return items


def load_candles(timeframe: str) -> list[Candle]:
    path = REPO_ROOT / f"{INSTRUMENT}_{timeframe}_{DATA_COUNT}_confirmed.csv"
    if not path.exists():
        raise FileNotFoundError(f"找不到数据文件：{path}")

    expected_step = TIMEFRAMES_MS[timeframe]
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != CSV_HEADER:
            raise DataValidationError(
                f"{path.name} 的列结构不是当前北京时间格式；期望 {CSV_HEADER}，实际 {reader.fieldnames}"
            )
        for row in reader:
            if row["confirm"] != "1":
                raise DataValidationError(f"{path.name} 包含未收盘 K 线")
            timestamp_ms = int(row["timestamp_ms"])
            if row["open_time_beijing"] != beijing_time(timestamp_ms):
                raise DataValidationError(f"{path.name} 的北京时间与 timestamp_ms 不一致：{timestamp_ms}")
            candles.append(
                Candle(
                    timestamp_ms=timestamp_ms,
                    time_beijing=row["open_time_beijing"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )

    if len(candles) != DATA_COUNT:
        raise DataValidationError(f"{path.name} 应有 {DATA_COUNT} 行，实际 {len(candles)} 行")
    timestamps = [candle.timestamp_ms for candle in candles]
    if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
        raise DataValidationError(f"{path.name} 存在乱序或重复时间戳")
    if any(current - previous != expected_step for previous, current in zip(timestamps, timestamps[1:])):
        raise DataValidationError(f"{path.name} 存在时间间隔缺口")
    return candles


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("EMA 周期必须大于 0")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def divergence_flags(
    closes: list[float],
    dif: list[float],
    pivot_window: int,
    max_pivot_distance: int,
) -> tuple[list[bool], list[bool]]:
    """Return divergence flags only after right-side pivot confirmation is known.

    A pivot at p is recognized at p + pivot_window, so the signal intentionally
    waits for future bars that would have been available at that later time.
    This avoids treating a yet-unconfirmed swing point as known at p.
    """
    size = len(closes)
    bullish = [False] * size
    bearish = [False] * size
    low_pivots: list[int] = []
    high_pivots: list[int] = []

    for pivot in range(pivot_window, size - pivot_window):
        price_window = closes[pivot - pivot_window : pivot + pivot_window + 1]
        current_price = closes[pivot]
        confirmed_at = pivot + pivot_window

        # Strict comparison avoids duplicate flat-range pivots.
        if current_price == min(price_window) and price_window.count(current_price) == 1:
            if low_pivots:
                previous = low_pivots[-1]
                if pivot - previous <= max_pivot_distance:
                    if closes[pivot] < closes[previous] and dif[pivot] > dif[previous]:
                        bullish[confirmed_at] = True
            low_pivots.append(pivot)

        if current_price == max(price_window) and price_window.count(current_price) == 1:
            if high_pivots:
                previous = high_pivots[-1]
                if pivot - previous <= max_pivot_distance:
                    if closes[pivot] > closes[previous] and dif[pivot] < dif[previous]:
                        bearish[confirmed_at] = True
            high_pivots.append(pivot)

    return bullish, bearish


def build_indicators(candles: list[Candle], params: StrategyParams) -> Indicators:
    closes = [candle.close for candle in candles]
    fast_ema = ema(closes, params.fast_period)
    slow_ema = ema(closes, params.slow_period)
    dif = [fast - slow for fast, slow in zip(fast_ema, slow_ema)]
    dea = ema(dif, params.signal_period)
    histogram = [line - signal for line, signal in zip(dif, dea)]
    size = len(candles)
    golden = [False] * size
    dead = [False] * size
    zero_up = [False] * size
    zero_down = [False] * size
    for index in range(1, size):
        golden[index] = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
        dead[index] = dif[index - 1] >= dea[index - 1] and dif[index] < dea[index]
        zero_up[index] = dif[index - 1] <= 0 < dif[index]
        zero_down[index] = dif[index - 1] >= 0 > dif[index]

    bullish_divergence, bearish_divergence = divergence_flags(
        closes, dif, params.pivot_window, params.divergence_lookback
    )
    return Indicators(
        dif=dif,
        dea=dea,
        histogram=histogram,
        golden_cross=golden,
        dead_cross=dead,
        zero_up=zero_up,
        zero_down=zero_down,
        bullish_divergence=bullish_divergence,
        bearish_divergence=bearish_divergence,
    )


def reduce_units_for_rule(current_units: int) -> int:
    """Implement the position rule: 1 all out; otherwise half, odd rounded up."""
    if current_units <= 0:
        return 0
    if current_units == 1:
        return 1
    return math.ceil(current_units / 2)


def bars_for_hours(timeframe: str, hours: float) -> int:
    return max(1, math.ceil(hours * 60 * 60 * 1000 / TIMEFRAMES_MS[timeframe]))


def annual_bars(timeframe: str) -> float:
    return 365.0 * 24.0 * 60.0 * 60.0 * 1000.0 / TIMEFRAMES_MS[timeframe]


def expansion_run(histogram: list[float], index: int, side: str, bars: int) -> bool:
    """Detect the first qualifying continuous histogram-expansion run."""
    if bars < 2 or index - bars + 1 < 0:
        return False
    segment = histogram[index - bars + 1 : index + 1]
    if side == "long":
        return all(value > 0 for value in segment) and all(next_value > value for value, next_value in zip(segment, segment[1:]))
    return all(value < 0 for value in segment) and all(abs(next_value) > abs(value) for value, next_value in zip(segment, segment[1:]))


def expansion_starts(histogram: list[float], index: int, side: str, bars: int) -> bool:
    return expansion_run(histogram, index, side, bars) and not expansion_run(histogram, index - 1, side, bars)


def shrinking_starts(histogram: list[float], index: int, side: str) -> bool:
    if index < 1:
        return False
    previous, current = histogram[index - 1], histogram[index]
    if side == "long":
        is_shrinking = previous > 0 and current > 0 and current < previous
        was_shrinking = (
            index >= 2
            and histogram[index - 2] > 0
            and previous > 0
            and previous < histogram[index - 2]
        )
    else:
        is_shrinking = previous < 0 and current < 0 and abs(current) < abs(previous)
        was_shrinking = (
            index >= 2
            and histogram[index - 2] < 0
            and previous < 0
            and abs(previous) < abs(histogram[index - 2])
        )
    return is_shrinking and not was_shrinking


class PortfolioEngine:
    """A lot-based hedge-mode simulation of the documented position rules."""

    def __init__(
        self,
        candles: list[Candle],
        indicators: Indicators,
        timeframe: str,
        params: StrategyParams,
        config: BacktestConfig,
        start_index: int,
        end_index: int,
        collect_details: bool,
    ) -> None:
        self.candles = candles
        self.indicators = indicators
        self.timeframe = timeframe
        self.params = params
        self.config = config
        self.start_index = start_index
        self.end_index = end_index
        self.collect_details = collect_details
        self.lots: dict[str, list[Lot]] = {"long": [], "short": []}
        self.cash = config.initial_capital_usdt
        self.total_fees = 0.0
        self.closed_net_results: list[float] = []
        self.events: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.skipped_entry_actions = 0
        self.forced_end_closures = 0
        self.last_activity_bar = {"long": start_index, "short": start_index}
        self.pending_bull_divergence_until = -1
        self.pending_bear_divergence_until = -1
        self.time_stop_bars = bars_for_hours(timeframe, config.time_stop_hours)

    @property
    def fee_rate(self) -> float:
        return self.config.fee_bps / 10_000.0

    @property
    def slippage_rate(self) -> float:
        return self.config.slippage_bps / 10_000.0

    def units(self, side: str) -> int:
        return len(self.lots[side])

    def total_units(self) -> int:
        return self.units("long") + self.units("short")

    def adjusted_price(self, raw_price: float, side: str, opening: bool) -> float:
        """Apply adverse slippage by the actual buy/sell direction."""
        buy = (side == "long" and opening) or (side == "short" and not opening)
        return raw_price * (1.0 + self.slippage_rate if buy else 1.0 - self.slippage_rate)

    def log_event(
        self,
        candle: Candle,
        event_type: str,
        side: str,
        reason: str,
        units: int,
        quantity_sol: float,
        price: float,
        fee: float,
        realized_net_pnl: float | None,
    ) -> None:
        if not self.collect_details:
            return
        self.events.append(
            {
                "time_beijing": candle.time_beijing,
                "timestamp_ms": candle.timestamp_ms,
                "event_type": event_type,
                "side": side,
                "reason": reason,
                "units": units,
                "quantity_sol": quantity_sol,
                "execution_price": price,
                "fee_usdt": fee,
                "realized_net_pnl_usdt": "" if realized_net_pnl is None else realized_net_pnl,
                "long_units_after": self.units("long"),
                "short_units_after": self.units("short"),
                "total_units_after": self.total_units(),
            }
        )

    def can_open_one_unit(self, side: str) -> bool:
        side_units = self.units(side)
        other_side = "short" if side == "long" else "long"
        other_units = self.units(other_side)
        if side_units >= 4 or self.total_units() >= 6:
            return False
        # The rule checks the position-difference constraint when adding to an
        # existing same-side position. First opening unit is allowed to repair
        # an existing temporary imbalance.
        if side_units > 0 and abs((side_units + 1) - other_units) > 2:
            return False
        return True

    def open_one_unit(self, side: str, candle: Candle, bar_index: int, reason: str) -> bool:
        if not self.can_open_one_unit(side):
            self.skipped_entry_actions += 1
            return False
        execution_price = self.adjusted_price(candle.open, side, opening=True)
        quantity = self.config.unit_notional_usdt / execution_price
        fee = self.config.unit_notional_usdt * self.fee_rate
        lot = Lot(
            side=side,
            quantity_sol=quantity,
            entry_price=execution_price,
            entry_fee=fee,
            entry_timestamp_ms=candle.timestamp_ms,
            entry_time_beijing=candle.time_beijing,
            entry_bar=bar_index,
            entry_reason=reason,
        )
        self.lots[side].append(lot)
        self.cash -= fee
        self.total_fees += fee
        self.last_activity_bar[side] = bar_index
        self.log_event(candle, "entry", side, reason, 1, quantity, execution_price, fee, None)
        return True

    def close_units(
        self,
        side: str,
        unit_count: int,
        candle: Candle,
        bar_index: int,
        reason: str,
        raw_price: float,
        lot_policy: str = "fifo",
        apply_slippage: bool = True,
    ) -> int:
        available = self.units(side)
        target_count = min(max(unit_count, 0), available)
        if target_count == 0:
            return 0
        execution_price = self.adjusted_price(raw_price, side, opening=False) if apply_slippage else raw_price
        if lot_policy == "lifo":
            selected = self.lots[side][-target_count:]
            del self.lots[side][-target_count:]
        else:
            selected = self.lots[side][:target_count]
            del self.lots[side][:target_count]

        total_quantity = 0.0
        total_fee = 0.0
        total_net = 0.0
        for lot in selected:
            exit_notional = lot.quantity_sol * execution_price
            exit_fee = exit_notional * self.fee_rate
            gross_pnl = (
                lot.quantity_sol * (execution_price - lot.entry_price)
                if side == "long"
                else lot.quantity_sol * (lot.entry_price - execution_price)
            )
            net_pnl = gross_pnl - lot.entry_fee - exit_fee
            self.cash += gross_pnl - exit_fee
            self.total_fees += exit_fee
            self.closed_net_results.append(net_pnl)
            total_quantity += lot.quantity_sol
            total_fee += exit_fee
            total_net += net_pnl

        self.last_activity_bar[side] = bar_index
        self.log_event(candle, "exit", side, reason, target_count, total_quantity, execution_price, total_fee, total_net)
        return target_count

    def close_all(self, side: str, candle: Candle, bar_index: int, reason: str, raw_price: float) -> int:
        return self.close_units(side, self.units(side), candle, bar_index, reason, raw_price, "fifo")

    def average_entry_price(self, side: str) -> float | None:
        lots = self.lots[side]
        if not lots:
            return None
        total_quantity = sum(lot.quantity_sol for lot in lots)
        return sum(lot.entry_price * lot.quantity_sol for lot in lots) / total_quantity

    def process_take_profit(self, candle: Candle, bar_index: int) -> None:
        """Apply the rule's fixed percentage take-profit once per side per bar."""
        for side in ("long", "short"):
            unit_count = self.units(side)
            average_price = self.average_entry_price(side)
            if unit_count == 0 or average_price is None:
                continue
            if side == "long":
                target = average_price * (1.0 + self.params.take_profit_pct)
                if candle.open >= target:
                    fill_price = candle.open
                elif candle.high >= target:
                    fill_price = target
                else:
                    continue
            else:
                target = average_price * (1.0 - self.params.take_profit_pct)
                if candle.open <= target:
                    fill_price = candle.open
                elif candle.low <= target:
                    fill_price = target
                else:
                    continue

            # A pre-positioned limit TP is modeled at target/open without extra
            # adverse slippage; the configured fee remains charged.
            self.close_units(
                side,
                reduce_units_for_rule(unit_count),
                candle,
                bar_index,
                "fixed_take_profit",
                fill_price,
                lot_policy="fifo",
                apply_slippage=False,
            )

    def update_divergence_state(self, index: int) -> None:
        if self.indicators.bullish_divergence[index]:
            self.pending_bull_divergence_until = index + self.config.divergence_confirmation_bars
        if self.indicators.bearish_divergence[index]:
            self.pending_bear_divergence_until = index + self.config.divergence_confirmation_bars
        if index > self.pending_bull_divergence_until:
            self.pending_bull_divergence_until = -1
        if index > self.pending_bear_divergence_until:
            self.pending_bear_divergence_until = -1

    def action_at_close(self, index: int) -> list[Action]:
        """Turn closed-bar signals into next-open actions using documented priority."""
        self.update_divergence_state(index)
        golden = self.indicators.golden_cross[index]
        dead = self.indicators.dead_cross[index]

        # 1. Opposite-direction cross: strongest signal, flatten target side + flip.
        if golden:
            if self.units("short") > 0:
                reason = "bullish_divergence_confirmed_golden_cross" if self.pending_bull_divergence_until >= index else "golden_cross_flip"
                return [Action("flip", "long", reason)]
            if self.total_units() == 0:
                reason = "bullish_divergence_confirmed_golden_cross" if self.pending_bull_divergence_until >= index else "golden_cross_open"
                return [Action("open_or_add", "long", reason)]
        if dead:
            if self.units("long") > 0:
                reason = "bearish_divergence_confirmed_dead_cross" if self.pending_bear_divergence_until >= index else "dead_cross_flip"
                return [Action("flip", "short", reason)]
            if self.total_units() == 0:
                reason = "bearish_divergence_confirmed_dead_cross" if self.pending_bear_divergence_until >= index else "dead_cross_open"
                return [Action("open_or_add", "short", reason)]

        # 2. Histogram contraction is the signal set's half-position reduction.
        reductions: list[Action] = []
        if self.units("long") > 0 and shrinking_starts(self.indicators.histogram, index, "long"):
            reductions.append(Action("reduce", "long", "red_histogram_shrinking", reduce_units_for_rule(self.units("long"))))
        if self.units("short") > 0 and shrinking_starts(self.indicators.histogram, index, "short"):
            reductions.append(Action("reduce", "short", "green_histogram_shrinking", reduce_units_for_rule(self.units("short"))))
        if reductions:
            return reductions

        # 3. Zero-axis signals: flat -> open; same-side -> add; opposite side -> no action.
        if self.indicators.zero_up[index]:
            if self.total_units() == 0:
                return [Action("open_or_add", "long", "zero_axis_up_open")]
            if self.units("long") > 0 and self.units("short") == 0:
                return [Action("open_or_add", "long", "zero_axis_up_add")]
        if self.indicators.zero_down[index]:
            if self.total_units() == 0:
                return [Action("open_or_add", "short", "zero_axis_down_open")]
            if self.units("short") > 0 and self.units("long") == 0:
                return [Action("open_or_add", "short", "zero_axis_down_add")]

        # 4. Histogram expansion: adds only to an already-held same-side position.
        if (
            self.units("long") > 0
            and self.units("short") == 0
            and expansion_starts(self.indicators.histogram, index, "long", self.params.histogram_expansion_bars)
        ):
            return [Action("open_or_add", "long", "red_histogram_expansion_add")]
        if (
            self.units("short") > 0
            and self.units("long") == 0
            and expansion_starts(self.indicators.histogram, index, "short", self.params.histogram_expansion_bars)
        ):
            return [Action("open_or_add", "short", "green_histogram_expansion_add")]

        # 5. Last priority: one-unit 48-hour time exit, LIFO as specified.
        actions: list[Action] = []
        for side in ("long", "short"):
            if self.units(side) > 0 and index - self.last_activity_bar[side] >= self.time_stop_bars:
                actions.append(Action("time_stop", side, "time_stop_48h", 1))
        return actions

    def execute_action(self, action: Action, candle: Candle, bar_index: int) -> None:
        if action.kind == "open_or_add":
            self.open_one_unit(action.side, candle, bar_index, action.reason)
        elif action.kind == "flip":
            opposite = "short" if action.side == "long" else "long"
            self.close_all(opposite, candle, bar_index, f"{action.reason}_close_opposite", candle.open)
            self.open_one_unit(action.side, candle, bar_index, f"{action.reason}_open_one_unit")
        elif action.kind == "reduce":
            self.close_units(action.side, action.units, candle, bar_index, action.reason, candle.open, "fifo")
        elif action.kind == "time_stop":
            self.close_units(action.side, action.units, candle, bar_index, action.reason, candle.open, "lifo")
        else:
            raise ValueError(f"Unknown action: {action.kind}")

    def mark_equity(self, candle: Candle) -> float:
        equity = self.cash
        for side in ("long", "short"):
            for lot in self.lots[side]:
                mark_exit_price = self.adjusted_price(candle.close, side, opening=False)
                exit_fee = lot.quantity_sol * mark_exit_price * self.fee_rate
                unrealized = (
                    lot.quantity_sol * (mark_exit_price - lot.entry_price)
                    if side == "long"
                    else lot.quantity_sol * (lot.entry_price - mark_exit_price)
                )
                equity += unrealized - exit_fee
        return equity

    def append_equity(self, candle: Candle) -> None:
        equity = self.mark_equity(candle)
        self.equity_curve.append(
            {
                "time_beijing": candle.time_beijing,
                "timestamp_ms": candle.timestamp_ms,
                "equity_usdt": equity,
                "cash_usdt": self.cash,
                "long_units": self.units("long"),
                "short_units": self.units("short"),
                "total_units": self.total_units(),
            }
        )

    def close_end_of_test(self) -> None:
        if self.end_index <= self.start_index:
            return
        last_index = self.end_index - 1
        candle = self.candles[last_index]
        for side in ("long", "short"):
            count = self.units(side)
            if count:
                self.close_units(side, count, candle, last_index, "end_of_test", candle.close, "fifo")
                self.forced_end_closures += count
        self.append_equity(candle)

    def calculate_metrics(self) -> Metrics:
        final_equity = self.cash
        net_pnl = final_equity - self.config.initial_capital_usdt
        return_pct = net_pnl / self.config.initial_capital_usdt * 100.0
        equities = [row["equity_usdt"] for row in self.equity_curve]
        peak = self.config.initial_capital_usdt
        max_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)

        period_returns: list[float] = []
        for previous, current in zip(equities, equities[1:]):
            if previous != 0:
                period_returns.append(current / previous - 1.0)
        if len(period_returns) >= 2:
            std = statistics.pstdev(period_returns)
            sharpe = statistics.fmean(period_returns) / std * math.sqrt(annual_bars(self.timeframe)) if std > 0 else 0.0
        else:
            sharpe = 0.0

        wins = [result for result in self.closed_net_results if result > 0]
        losses = [result for result in self.closed_net_results if result < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (None if gross_profit == 0 else math.inf)
        closed_count = len(self.closed_net_results)
        win_rate = len(wins) / closed_count * 100.0 if closed_count else 0.0
        return Metrics(
            initial_capital_usdt=self.config.initial_capital_usdt,
            final_equity_usdt=final_equity,
            net_pnl_usdt=net_pnl,
            net_return_pct=return_pct,
            max_drawdown_pct=max_drawdown,
            annualized_sharpe=sharpe,
            closed_lots=closed_count,
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            total_fees_usdt=self.total_fees,
            skipped_entry_actions=self.skipped_entry_actions,
            forced_end_closures=self.forced_end_closures,
        )

    def run(self) -> BacktestRun:
        if self.end_index - self.start_index < 5:
            raise DataValidationError("回测区间过短")
        pending_actions: list[Action] = []
        for index in range(self.start_index, self.end_index):
            candle = self.candles[index]
            # Signals are observed on a completed prior bar and executed here,
            # at this bar's open price.
            for action in pending_actions:
                self.execute_action(action, candle, index)
            pending_actions = []

            # Fixed TP may fill inside this completed candle.
            self.process_take_profit(candle, index)
            self.append_equity(candle)

            # Generate action at current close for the next bar's open.
            if index + 1 < self.end_index:
                pending_actions = self.action_at_close(index)

        self.close_end_of_test()
        return BacktestRun(self.calculate_metrics(), self.events, self.equity_curve)


def ranking_score(metrics: Metrics, min_trades: int) -> float:
    """Optimize on train return minus drawdown, while penalizing sparse samples."""
    score = metrics.net_return_pct - metrics.max_drawdown_pct
    if metrics.closed_lots < min_trades:
        score -= (min_trades - metrics.closed_lots) * 2.0
    return score


def parameter_grid(args: argparse.Namespace) -> list[StrategyParams]:
    combinations: list[StrategyParams] = []
    for fast, slow, signal, take_profit, histogram_bars, divergence_lookback, pivot_window in itertools.product(
        args.fast_periods,
        args.slow_periods,
        args.signal_periods,
        args.take_profit_pcts,
        args.histogram_expansion_bars,
        args.divergence_lookbacks,
        args.pivot_windows,
    ):
        if fast <= 0 or slow <= 0 or fast >= slow:
            continue
        if (
            signal <= 0
            or take_profit <= 0
            or histogram_bars < 2
            or pivot_window < 1
            or divergence_lookback <= pivot_window * 2
        ):
            continue
        combinations.append(
            StrategyParams(
                fast_period=fast,
                slow_period=slow,
                signal_period=signal,
                take_profit_pct=take_profit,
                histogram_expansion_bars=histogram_bars,
                divergence_lookback=divergence_lookback,
                pivot_window=pivot_window,
            )
        )
    if not combinations:
        raise ValueError("没有有效参数组合；请确保 fast < slow，且各周期为正数")
    if len(combinations) > args.max_combinations and not args.allow_large_grid:
        raise ValueError(
            f"参数组合有 {len(combinations)} 个，超过安全上限 {args.max_combinations}。"
            "请缩小网格，或显式传入 --allow-large-grid。"
        )
    return combinations


def warmup_bars(params: StrategyParams) -> int:
    return max(params.slow_period + params.signal_period, params.divergence_lookback + params.pivot_window * 2) + 2


def evaluate_one(
    candles: list[Candle],
    timeframe: str,
    params: StrategyParams,
    config: BacktestConfig,
    collect_best_details: bool = False,
) -> tuple[OptimizationRow, BacktestRun | None]:
    indicators = build_indicators(candles, params)
    split_index = int(len(candles) * config.train_ratio)
    start_train = warmup_bars(params)
    if split_index - start_train < 200 or len(candles) - split_index < 200:
        raise DataValidationError("训练或样本外区间过短；请调整训练比例或数据长度")

    train_engine = PortfolioEngine(
        candles, indicators, timeframe, params, config, start_train, split_index, collect_details=False
    )
    train = train_engine.run()
    test_engine = PortfolioEngine(
        candles, indicators, timeframe, params, config, split_index, len(candles), collect_details=collect_best_details
    )
    test = test_engine.run()
    row = OptimizationRow(
        timeframe=timeframe,
        fast_period=params.fast_period,
        slow_period=params.slow_period,
        signal_period=params.signal_period,
        take_profit_pct=params.take_profit_pct,
        histogram_expansion_bars=params.histogram_expansion_bars,
        divergence_lookback=params.divergence_lookback,
        pivot_window=params.pivot_window,
        train_bars=split_index - start_train,
        test_bars=len(candles) - split_index,
        train_score=ranking_score(train.metrics, config.min_trades_for_ranking),
        train_return_pct=train.metrics.net_return_pct,
        train_max_drawdown_pct=train.metrics.max_drawdown_pct,
        train_sharpe=train.metrics.annualized_sharpe,
        train_closed_lots=train.metrics.closed_lots,
        train_win_rate_pct=train.metrics.win_rate_pct,
        train_profit_factor=train.metrics.profit_factor,
        test_return_pct=test.metrics.net_return_pct,
        test_max_drawdown_pct=test.metrics.max_drawdown_pct,
        test_sharpe=test.metrics.annualized_sharpe,
        test_closed_lots=test.metrics.closed_lots,
        test_win_rate_pct=test.metrics.win_rate_pct,
        test_profit_factor=test.metrics.profit_factor,
        test_net_pnl_usdt=test.metrics.net_pnl_usdt,
    )
    return row, test if collect_best_details else None


def atomic_write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temporary:
        writer = csv.DictWriter(temporary, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_name = temporary.name
    os.replace(temporary_name, path)


def markdown_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if math.isinf(value):
        return "∞"
    return f"{value:.{digits}f}"


def write_report(
    path: Path,
    results_by_timeframe: dict[str, list[OptimizationRow]],
    best_by_timeframe: dict[str, OptimizationRow],
    config: BacktestConfig,
    combination_count: int,
) -> None:
    lines = [
        "# MACD 信号集：参数优化回测报告",
        "",
        "## 回测范围与方法",
        "",
        "- 数据：仓库根目录的 `SOL-USDT-SWAP` 已收盘 K 线 CSV；所有可读时间为北京时间（UTC+08:00）。",
        "- 训练/样本外切分：按时间顺序前 " + f"{config.train_ratio:.0%}" + " 训练、后 " + f"{1 - config.train_ratio:.0%}" + " 样本外验证；两个区间的仓位独立重置。",
        "- 信号计算：以已收盘 K 线收盘价生成信号，下一根 K 线开盘执行；固定止盈在当根 OHLC 内检查。",
        "- 优化排序：训练集 `净收益率 − 最大回撤`，且低于最少成交笔数的组合会被扣分。",
        f"- 每个周期评估参数组合数：{combination_count}。",
        "",
        "## 已实现的 MACD 信号与规则映射",
        "",
        "| MACD 信号 | 回测动作 |",
        "|---|---|",
        "| 金叉 / 死叉 | 空仓开仓；持相反方向时全平相反方向后反手开 1 单 |",
        "| DIF 上穿 / 下穿零轴 | 空仓开仓；仅持同方向时按加仓规则加 1 单 |",
        "| 底背离 / 顶背离 | 仅作预警；在确认窗口内配合金叉 / 死叉才标记为强反转入场 |",
        "| 红柱 / 绿柱连续放大 | 仅对已持有的同方向加 1 单 |",
        "| 红柱 / 绿柱首次缩小 | 按仓位规则减仓：1 单全平，其余减半、奇数向上取整 |",
        "| 固定比例止盈 | 到达参数化 TP 目标后，按同一减仓规则减仓 |",
        "| 48 小时时间止损 | 无仓位活动达 48 小时后，按后进先出平最后 1 单 |",
        "",
        "## 财务与执行假设",
        "",
        f"- 每单固定名义价值：{config.unit_notional_usdt:.2f} USDT（按原规则的 200 U 默认值；不模拟杠杆）。",
        f"- 初始资金：{config.initial_capital_usdt:.2f} USDT。",
        f"- 手续费：{config.fee_bps:.2f} bps / 单边；滑点：{config.slippage_bps:.2f} bps。",
        "- 支持多空同时持仓；单向最多 4 单、总仓最多 6 单；同方向加仓后仓位差不得超过 2 单。",
        "- 未计入资金费、强平、爆仓、盘口深度、部分成交、实际限价单未成交和税费。",
        "",
        "## 各周期最优训练参数与样本外结果",
        "",
        "| 周期 | MACD（快/慢/信号） | TP | 柱扩张根数 | 训练分数 | 训练收益% | 训练回撤% | 样本外收益% | 样本外回撤% | 样本外 Sharpe | 样本外成交单位 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for timeframe, result in best_by_timeframe.items():
        lines.append(
            "| {tf} | {fast}/{slow}/{signal} | {tp:.2%} | {hist} | {score:.2f} | {tr:.2f} | {tdd:.2f} | {te:.2f} | {tedd:.2f} | {sharpe:.2f} | {trades} |".format(
                tf=timeframe,
                fast=result.fast_period,
                slow=result.slow_period,
                signal=result.signal_period,
                tp=result.take_profit_pct,
                hist=result.histogram_expansion_bars,
                score=result.train_score,
                tr=result.train_return_pct,
                tdd=result.train_max_drawdown_pct,
                te=result.test_return_pct,
                tedd=result.test_max_drawdown_pct,
                sharpe=result.test_sharpe,
                trades=result.test_closed_lots,
            )
        )

    for timeframe, rows in results_by_timeframe.items():
        lines.extend(["", f"## {timeframe}：训练集排名前 5 的参数组合", ""])
        lines.extend(
            [
                "| 排名 | 快线 | 慢线 | 信号线 | TP | 柱扩张根数 | 训练分数 | 训练收益% | 训练回撤% | 样本外收益% | 样本外回撤% |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, result in enumerate(rows[:5], start=1):
            lines.append(
                "| {rank} | {fast} | {slow} | {signal} | {tp:.2%} | {hist} | {score:.2f} | {tr:.2f} | {tdd:.2f} | {te:.2f} | {tedd:.2f} |".format(
                    rank=rank,
                    fast=result.fast_period,
                    slow=result.slow_period,
                    signal=result.signal_period,
                    tp=result.take_profit_pct,
                    hist=result.histogram_expansion_bars,
                    score=result.train_score,
                    tr=result.train_return_pct,
                    tdd=result.train_max_drawdown_pct,
                    te=result.test_return_pct,
                    tedd=result.test_max_drawdown_pct,
                )
            )

    lines.extend(
        [
            "",
            "## 输出文件说明",
            "",
            "- `macd_parameter_optimization_results.csv`：所有周期、所有参数组合的训练与样本外指标。",
            "- `macd_best_trades_<周期>.csv`：该周期训练集最优参数在样本外区间的逐笔成交记录。",
            "- `macd_best_equity_<周期>.csv`：该周期训练集最优参数在样本外区间的逐根权益曲线。",
            "",
            "## 风险提示",
            "",
            "参数优化会产生过拟合风险。应优先观察样本外收益、回撤、成交数量和不同周期的一致性，不应仅依据训练集排名或单次回测结果进行真实交易。",
        ]
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as temporary:
        temporary.write("\n".join(lines) + "\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_name = temporary.name
    os.replace(temporary_name, path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MACD 信号集参数优化回测器（北京时间数据）。")
    parser.add_argument("--timeframes", type=parse_timeframes, default=list(TIMEFRAMES_MS), help="周期列表，如 15m,30m,1H,2H")
    parser.add_argument("--fast-periods", type=parse_int_list, default=[8, 10, 12, 14], help="快线 EMA 网格")
    parser.add_argument("--slow-periods", type=parse_int_list, default=[21, 26, 30, 35], help="慢线 EMA 网格")
    parser.add_argument("--signal-periods", type=parse_int_list, default=[6, 9, 12], help="DEA EMA 网格")
    parser.add_argument("--take-profit-pcts", type=parse_float_list, default=[0.015, 0.02, 0.025], help="止盈比例网格，如 0.015,0.02")
    parser.add_argument("--histogram-expansion-bars", type=parse_int_list, default=[2, 3], help="柱状连续放大根数网格")
    parser.add_argument("--divergence-lookbacks", type=parse_int_list, default=[30], help="背离两个枢轴的最大间隔根数")
    parser.add_argument("--pivot-windows", type=parse_int_list, default=[3], help="背离枢轴左右确认根数")
    parser.add_argument("--train-ratio", type=float, default=0.70, help="训练集占比，默认 0.70")
    parser.add_argument("--unit-notional-usdt", type=float, default=200.0, help="每单固定名义价值，默认 200 USDT")
    parser.add_argument("--initial-capital-usdt", type=float, default=10_000.0, help="初始资金，默认 10000 USDT")
    parser.add_argument("--fee-bps", type=float, default=2.0, help="单边手续费（bps），默认 2")
    parser.add_argument("--slippage-bps", type=float, default=0.0, help="开/平仓不利滑点（bps），默认 0")
    parser.add_argument("--time-stop-hours", type=float, default=48.0, help="时间止损小时数，默认 48")
    parser.add_argument("--divergence-confirmation-bars", type=int, default=12, help="背离等待金叉/死叉确认的有效根数")
    parser.add_argument("--min-trades-for-ranking", type=int, default=10, help="训练排名的最少已平单位数")
    parser.add_argument("--top-n", type=int, default=10, help="报告和 CSV 的每周期排名标记数量")
    parser.add_argument("--max-combinations", type=int, default=400, help="每周期安全参数组合上限")
    parser.add_argument("--allow-large-grid", action="store_true", help="允许超过安全上限的组合")
    parser.add_argument("--output-dir", type=Path, default=SCRIPT_DIR, help="报告和 CSV 输出目录，默认 signal_sets/")
    parser.add_argument("--dry-run", action="store_true", help="执行回测但不写入报告、CSV 或成交明细")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 0.5 <= args.train_ratio < 0.9:
        raise ValueError("--train-ratio 必须在 [0.5, 0.9) 内")
    for name in ("unit_notional_usdt", "initial_capital_usdt", "time_stop_hours"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 必须大于 0")
    if args.fee_bps < 0 or args.slippage_bps < 0:
        raise ValueError("手续费和滑点不能为负数")
    if args.divergence_confirmation_bars < 1 or args.min_trades_for_ranking < 0:
        raise ValueError("确认窗口和最少成交数必须有效")


def main() -> int:
    args = build_arg_parser().parse_args()
    validate_args(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = BacktestConfig(
        unit_notional_usdt=args.unit_notional_usdt,
        initial_capital_usdt=args.initial_capital_usdt,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        time_stop_hours=args.time_stop_hours,
        divergence_confirmation_bars=args.divergence_confirmation_bars,
        train_ratio=args.train_ratio,
        min_trades_for_ranking=args.min_trades_for_ranking,
    )
    grid = parameter_grid(args)
    print(f"每个周期将评估 {len(grid)} 个有效参数组合。")

    all_rows: list[OptimizationRow] = []
    results_by_timeframe: dict[str, list[OptimizationRow]] = {}
    best_by_timeframe: dict[str, OptimizationRow] = {}
    best_details: dict[str, BacktestRun] = {}

    for timeframe in args.timeframes:
        candles = load_candles(timeframe)
        rows: list[OptimizationRow] = []
        print(f"\n[{timeframe}] 加载 {len(candles)} 根 K 线，开始参数优化…")
        for index, params in enumerate(grid, start=1):
            row, _ = evaluate_one(candles, timeframe, params, config, collect_best_details=False)
            rows.append(row)
            if index % 50 == 0 or index == len(grid):
                print(f"[{timeframe}] 已完成 {index}/{len(grid)} 组合", flush=True)

        rows.sort(key=lambda row: (row.train_score, row.train_return_pct, -row.train_max_drawdown_pct), reverse=True)
        for row in rows[: args.top_n]:
            row.is_best_for_timeframe = row is rows[0]
        best = rows[0]
        best_params = StrategyParams(
            fast_period=best.fast_period,
            slow_period=best.slow_period,
            signal_period=best.signal_period,
            take_profit_pct=best.take_profit_pct,
            histogram_expansion_bars=best.histogram_expansion_bars,
            divergence_lookback=best.divergence_lookback,
            pivot_window=best.pivot_window,
        )
        _, detail = evaluate_one(candles, timeframe, best_params, config, collect_best_details=True)
        assert detail is not None
        results_by_timeframe[timeframe] = rows
        best_by_timeframe[timeframe] = best
        best_details[timeframe] = detail
        all_rows.extend(rows)
        print(
            f"[{timeframe}] 最优：MACD {best.fast_period}/{best.slow_period}/{best.signal_period}，"
            f"TP {best.take_profit_pct:.2%}，训练分数 {best.train_score:.2f}，"
            f"样本外收益 {best.test_return_pct:.2f}%"
        )

    if args.dry_run:
        print("\nDry run 完成：未写入报告或 CSV 文件。")
        return 0

    result_path = args.output_dir / "macd_parameter_optimization_results.csv"
    result_fields = list(asdict(all_rows[0]).keys())
    atomic_write_csv(result_path, result_fields, (asdict(row) for row in all_rows))
    report_path = args.output_dir / "macd_backtest_report.md"
    write_report(report_path, results_by_timeframe, best_by_timeframe, config, len(grid))

    for timeframe, detail in best_details.items():
        trade_path = args.output_dir / f"macd_best_trades_{timeframe}.csv"
        equity_path = args.output_dir / f"macd_best_equity_{timeframe}.csv"
        trade_fields = [
            "time_beijing", "timestamp_ms", "event_type", "side", "reason", "units", "quantity_sol",
            "execution_price", "fee_usdt", "realized_net_pnl_usdt", "long_units_after", "short_units_after", "total_units_after",
        ]
        equity_fields = ["time_beijing", "timestamp_ms", "equity_usdt", "cash_usdt", "long_units", "short_units", "total_units"]
        atomic_write_csv(trade_path, trade_fields, detail.events)
        atomic_write_csv(equity_path, equity_fields, detail.equity_curve)

    print("\n回测完成，已写入：")
    print(f"- {result_path}")
    print(f"- {report_path}")
    for timeframe in best_details:
        print(f"- {args.output_dir / f'macd_best_trades_{timeframe}.csv'}")
        print(f"- {args.output_dir / f'macd_best_equity_{timeframe}.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
