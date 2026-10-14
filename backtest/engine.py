"""
回测引擎 - 逐K线走马灯（保证精确性）
"""

from typing import Optional, Callable, Dict, List
import pandas as pd
from position_manager_v3 import PositionManagerV3, PositionSide
from stats_collector import StatsCollector


class BacktestEngine:
    """
    回测引擎 - 逐K线遍历
    """
    
    def __init__(self, data: pd.DataFrame, position_manager: PositionManagerV3,
                 stats_collector: StatsCollector,
                 strategy: Callable[[pd.Series, PositionManagerV3], Optional[Dict]]):
        """
        参数:
            data: K线数据 (需包含: timestamp, open, high, low, close, volume)
            position_manager: 仓位管理器
            stats_collector: 统计收集器
            strategy: 策略函数 (bar -> Optional[action])
        """
        self.data = data.reset_index(drop=True)
        self.pm = position_manager
        self.stats = stats_collector
        self.strategy = strategy
        
        self._current_bar: int = 0
        self._entry_bar: int = 0
        self._entry_price: float = 0
        self._entry_time: int = 0
    
    def run(self) -> StatsCollector:
        """运行回测"""
        stats = self.stats.create_stats()
        initial_balance = self.pm.account_balance
        
        for i in range(len(self.data)):
            self._current_bar = i
            bar = self.data.iloc[i]
            
            signal = self.strategy(bar, self.pm)
            
            if signal:
                action = signal.get('action')
                
                if action == 'open_long' and self.pm.can_open(bar['close']):
                    result = self.pm.open_long(bar['close'], bar['timestamp'])
                    if result:
                        self._entry_bar = i
                        self._entry_price = bar['close']
                        self._entry_time = bar['timestamp']
                
                elif action == 'add_long' and self.pm.can_add(bar['close']):
                    self.pm.add_long(bar['close'], bar['timestamp'])
                
                elif action == 'close_long' and self.pm.position.is_open:
                    result = self.pm.close_long(bar['close'], bar['timestamp'])
                    if result:
                        pnl = result['pnl']
                        stats.record_trade(
                            entry_price=self._entry_price,
                            exit_price=bar['close'],
                            entry_time=self._entry_time,
                            exit_time=bar['timestamp'],
                            side='long',
                            pnl=pnl,
                            holding_bars=i - self._entry_bar,
                            initial_balance=initial_balance
                        )
                        stats.update_balance(self.pm.account_balance)
            
            if self.pm.position.is_open:
                price = bar['close']
                if self.pm.check_stop_loss(price):
                    result = self.pm.close_long(price, bar['timestamp'])
                    if result:
                        pnl = result['pnl']
                        stats.record_trade(
                            entry_price=self._entry_price,
                            exit_price=price,
                            entry_time=self._entry_time,
                            exit_time=bar['timestamp'],
                            side='long',
                            pnl=pnl,
                            holding_bars=i - self._entry_bar,
                            initial_balance=initial_balance
                        )
                        stats.update_balance(self.pm.account_balance)
        
        stats.finalize(initial_balance)
        return stats
    
    @property
    def current_bar_index(self) -> int:
        return self._current_bar
