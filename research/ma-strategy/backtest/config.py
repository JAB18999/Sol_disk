"""回测配置与参数空间（MA 双均线策略）。

所有参数严格遵循预注册 research/ma-strategy/preregistration-v1.md，
不得擅自增减参数搜索空间或更改评价指标计算方式。
"""

from __future__ import annotations

import os
from typing import Dict, List

TIMEZONE = "Asia/Shanghai"

# 此文件位于 research/ma-strategy/backtest/config.py，向上三级为仓库根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA_DIR = os.path.join(ROOT_DIR, "DATA")
ARTIFACT_DIR = os.path.join(ROOT_DIR, "research", "ma-strategy", "backtest", "artifacts")

# 粒度定义：csv 文件名、分钟数、年化根数
GRAIN: List[Dict] = [
    {"name": "5m", "filename": "SOL-USDT-SWAP-5m.csv", "minutes": 5,  "bars_per_year": 365 * 24 * 12},
    {"name": "15m", "filename": "SOL-USDT-SWAP-15m.csv", "minutes": 15, "bars_per_year": 365 * 24 * 4},
    {"name": "30m", "filename": "SOL-USDT-SWAP-30m.csv", "minutes": 30, "bars_per_year": 365 * 24 * 2},
    {"name": "1H",  "filename": "SOL-USDT-SWAP-1H.csv",  "minutes": 60, "bars_per_year": 365 * 24},
    {"name": "2H",  "filename": "SOL-USDT-SWAP-2H.csv",  "minutes": 120, "bars_per_year": 365 * 12},
]

# 参数空间：20 组（5 粒度 × 每粒度 4 组），严格按预注册表格
PARAM_GRID: List[Dict] = [
    # 5m：快 12/24/36/48，慢 144/288/432/576，比例 1:12，慢周期 12/24/36/48h
    {"grain": "5m",  "fast": 12, "slow": 144},
    {"grain": "5m",  "fast": 24, "slow": 288},
    {"grain": "5m",  "fast": 36, "slow": 432},
    {"grain": "5m",  "fast": 48, "slow": 576},
    # 15m：快 6/12/18/24，慢 48/96/144/192，比例 1:8，慢周期 12/24/36/48h
    {"grain": "15m", "fast": 6,  "slow": 48},
    {"grain": "15m", "fast": 12, "slow": 96},
    {"grain": "15m", "fast": 18, "slow": 144},
    {"grain": "15m", "fast": 24, "slow": 192},
    # 30m：快 4/8/12/16，慢 24/48/72/96，比例 1:6，慢周期 12/24/36/48h
    {"grain": "30m", "fast": 4,  "slow": 24},
    {"grain": "30m", "fast": 8,  "slow": 48},
    {"grain": "30m", "fast": 12, "slow": 72},
    {"grain": "30m", "fast": 16, "slow": 96},
    # 1H：快 5/7/9/11，慢 20/28/36/44，比例 1:4，慢周期 20/28/36/44h
    {"grain": "1H",  "fast": 5,  "slow": 20},
    {"grain": "1H",  "fast": 7,  "slow": 28},
    {"grain": "1H",  "fast": 9,  "slow": 36},
    {"grain": "1H",  "fast": 11, "slow": 44},
    # 2H：快 5/7/9/11，慢 10/14/18/22，比例 1:2，慢周期 20/28/36/44h
    {"grain": "2H",  "fast": 5,  "slow": 10},
    {"grain": "2H",  "fast": 7,  "slow": 14},
    {"grain": "2H",  "fast": 9,  "slow": 18},
    {"grain": "2H",  "fast": 11, "slow": 22},
]

# 样本区间（UTC+8 / 北京时间，两端闭区间）
SAMPLE_SPLITS: List[Dict] = [
    {"name": "train",  "start": "2025-11-11 00:00:00", "end": "2026-06-30 23:59:00"},
    {"name": "validate", "start": "2026-07-01 00:00:00", "end": "2026-08-15 23:59:00"},
    {"name": "test",   "start": "2026-08-16 00:00:00", "end": "2026-11-06 23:59:00"},
]

# 资金与风控
INITIAL_EQUITY = 100000.0
POSITION_FRACTION = 0.8
STOP_LOSS_PCT = 0.03
FEE_RATE = 0.0
SLIPPAGE = 0.0

# 熔断 / 规则 B 阈值
BREACH_MDD_THRESHOLD = 0.20
RULE_B_SHARPE_MIN = 0.0
RULE_B_MDD_MAX = 0.20


def slow_period_hours(fast: int, slow: int, grain: str) -> float:
    """返回慢周期实际小时数（基于粒度分钟数）。"""
    minutes_map = {g["name"]: g["minutes"] for g in GRAIN}
    if grain not in minutes_map:
        raise ValueError(f"未知粒度: {grain}")
    minutes = minutes_map[grain]
    return slow * minutes / 60.0


def apply_rule_a(param_grid: List[Dict] = None) -> List[Dict]:
    """规则 A：过滤掉慢周期实际时间不在 12-48h 的参数组合。"""
    if param_grid is None:
        param_grid = PARAM_GRID
    filtered = []
    for p in param_grid:
        h = slow_period_hours(p["fast"], p[slow"], p["grain"])
        if 12.0 <= h <= 48.0:
            filtered.append(p)
    return filtered


def grid_size() -> int:
    """返回参数空间总组数（预注册要求为 20）。"""
    return len(PARAM_GRID)


def grain_by_name(name: str) -> Dict:
    """按粒度名查找 GRAIN 定义。"""
    for g in GRAIN:
        if g["name"] == name:
            return g
    raise ValueError(f"未知粒度: {name}")


def split_by_name(name: str) -> Dict:
    """按阶段名查找 SAMPLE_SPLITS 定义。"""
    for s in SAMPLE_SPLITS:
        if s["name"] == name:
            return s
    raise ValueError(f"未知阶段: {name}")