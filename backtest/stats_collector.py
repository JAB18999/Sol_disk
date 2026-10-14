"""
统计收集器 - 自动适配单次回测/参数优化模式
"""

from typing import Optional, Dict, List
from dataclasses import dataclass, field
from enum import Enum


class TradeType(Enum):
    WIN = "win"
    LOSS = "loss"
    BREAK_EVEN = "break_even"


@dataclass
class TradeRecord:
    """交易记录"""
    entry_price: float
    exit_price: float
    entry_time: int
    exit_time: int
    side: str
    pnl: float
    pnl_pct: float
    holding_bars: int
    trade_type: TradeType = TradeType.BREAK_EVEN


class DetailedStatsCollector:
    """详细统计收集器 - 单次回测用"""
    
    def __init__(self):
        self.trades: List[TradeRecord] = []
        self.total_pnl: float = 0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.max_drawdown: float = 0
        self.max_drawdown_pct: float = 0
        self.peak_balance: float = 0
        self.final_balance: float = 0
        self.initial_balance: float = 0
        self.total_holding_bars: int = 0
    
    def record_trade(self, entry_price: float, exit_price: float, entry_time: int, 
                    exit_time: int, side: str, pnl: float, holding_bars: int,
                    initial_balance: float):
        """记录交易"""
        pnl_pct = pnl / initial_balance * 100 if initial_balance > 0 else 0
        
        if pnl > 0:
            trade_type = TradeType.WIN
            self.winning_trades += 1
        elif pnl < 0:
            trade_type = TradeType.LOSS
            self.losing_trades += 1
        else:
            trade_type = TradeType.BREAK_EVEN
        
        trade = TradeRecord(
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            side=side,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_bars=holding_bars,
            trade_type=trade_type
        )
        self.trades.append(trade)
        self.total_pnl += pnl
        self.total_trades += 1
        self.total_holding_bars += holding_bars
    
    def update_balance(self, balance: float):
        """更新余额（用于计算回撤）"""
        if balance > self.peak_balance:
            self.peak_balance = balance
        
        drawdown = self.peak_balance - balance
        drawdown_pct = drawdown / self.peak_balance * 100 if self.peak_balance > 0 else 0
        
        if drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown = drawdown
            self.max_drawdown_pct = drawdown_pct
        
        self.final_balance = balance
    
    def finalize(self, initial_balance: float):
        """完成统计"""
        self.initial_balance = initial_balance
        self.final_balance = self.total_pnl + initial_balance
    
    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades * 100 if self.total_trades > 0 else 0
    
    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.trade_type == TradeType.WIN]
        return sum(wins) / len(wins) if wins else 0
    
    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.trade_type == TradeType.LOSS]
        return sum(losses) / len(losses) if losses else 0
    
    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.trades if t.trade_type == TradeType.WIN)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.trade_type == TradeType.LOSS))
        return gross_win / gross_loss if gross_loss > 0 else float('inf')
    
    @property
    def avg_holding_bars(self) -> float:
        return self.total_holding_bars / self.total_trades if self.total_trades > 0 else 0
    
    def summary(self) -> Dict:
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': f"{self.win_rate:.2f}%",
            'total_pnl': f"{self.total_pnl:.2f}",
            'profit_factor': f"{self.profit_factor:.2f}" if self.profit_factor != float('inf') else "inf",
            'avg_win': f"{self.avg_win:.2f}",
            'avg_loss': f"{self.avg_loss:.2f}",
            'max_drawdown': f"{self.max_drawdown:.2f} ({self.max_drawdown_pct:.2f}%)",
            'avg_holding_bars': f"{self.avg_holding_bars:.1f}",
            'final_balance': f"{self.final_balance:.2f}",
            'initial_balance': f"{self.initial_balance:.2f}",
        }
    
    def print_summary(self):
        """打印统计报告"""
        print("\n" + "="*60)
        print("回测统计报告")
        print("="*60)
        for key, value in self.summary().items():
            print(f"  {key}: {value}")
        print("="*60)


class AggregatedStatsCollector:
    """聚合统计收集器 - 参数优化用（精简输出）"""
    
    def __init__(self):
        self.total_pnl: float = 0
        self.total_trades: int = 0
        self.max_drawdown_pct: float = 0
        self.win_rate: float = 0
        self.profit_factor: float = 0
        self.avg_holding_bars: float = 0
    
    def update_from_detailed(self, stats: DetailedStatsCollector):
        """从详细统计更新"""
        self.total_pnl += stats.total_pnl
        self.total_trades += stats.total_trades
        if stats.max_drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = stats.max_drawdown_pct
        self.win_rate = (self.win_rate + stats.win_rate) / 2
        self.avg_holding_bars = stats.avg_holding_bars


class StatsCollector:
    """
    统计收集器 - 根据回测次数自动适配
    - 单次回测 → DetailedStatsCollector
    - 参数优化(>50次) → AggregatedStatsCollector
    """
    
    def __init__(self, mode: str = 'auto'):
        self.mode = mode
        self.stats: Optional[DetailedStatsCollector] = None
        self.agg_stats: Optional[AggregatedStatsCollector] = None
        self._backtest_count = 0
    
    def create_stats(self) -> DetailedStatsCollector:
        """创建详细统计收集器"""
        self.stats = DetailedStatsCollector()
        return self.stats
    
    def increment_backtest(self):
        """增加回测计数"""
        self._backtest_count += 1
    
    def get_stats(self) -> DetailedStatsCollector:
        """获取当前统计"""
        return self.stats
    
    def is_optimization_mode(self) -> bool:
        """是否参数优化模式"""
        return self._backtest_count > 50
    
    def print_summary(self):
        """打印统计报告"""
        if self.stats:
            self.stats.print_summary()
