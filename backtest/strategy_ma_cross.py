"""双均线交叉策略"""
from typing import Optional, Dict
import pandas as pd

class MACrossStrategy:
    def __init__(self, fast_period: int = 10, slow_period: int = 30):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.prices = []
        self.last_signal = None
    
    def on_bar(self, bar: pd.Series, position_manager) -> Optional[Dict]:
        close = bar['close']
        self.prices.append(close)
        
        if len(self.prices) < self.slow_period:
            return None
        
        fast_ma = sum(self.prices[-self.fast_period:]) / self.fast_period
        slow_ma = sum(self.prices[-self.slow_period:]) / self.slow_period
        
        prev_fast_ma = sum(self.prices[-self.fast_period-1:-1]) / self.fast_period if len(self.prices) > self.fast_period else close
        prev_slow_ma = sum(self.prices[-self.slow_period-1:-1]) / self.slow_period if len(self.prices) > self.slow_period else close
        
        current_signal = None
        
        if prev_fast_ma <= prev_slow_ma and fast_ma > slow_ma:
            current_signal = 'golden_cross'
        elif prev_fast_ma >= prev_slow_ma and fast_ma < slow_ma:
            current_signal = 'death_cross'
        
        if current_signal == 'golden_cross':
            if not position_manager.position.is_open:
                self.last_signal = 'long'
                return {'action': 'open_long'}
        elif current_signal == 'death_cross':
            if position_manager.position.is_open:
                self.last_signal = 'short'
                return {'action': 'close_long'}
        
        return None

def create_strategy(fast_period: int = 10, slow_period: int = 30):
    strategy = MACrossStrategy(fast_period, slow_period)
    return strategy.on_bar
