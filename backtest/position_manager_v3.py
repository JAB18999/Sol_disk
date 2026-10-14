"""
仓位管理器 V3 - 固定金额 + 固定仓位数量模式
"""

from typing import Optional, Dict, List
from dataclasses import dataclass, field
from enum import Enum


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass
class Position:
    """持仓信息"""
    side: PositionSide = PositionSide.FLAT
    entries: List[Dict] = field(default_factory=list)
    total_value: float = 0.0
    avg_price: float = 0.0
    count: int = 0
    
    @property
    def is_open(self) -> bool:
        return self.side != PositionSide.FLAT


@dataclass
class PositionManagerV3:
    """
    仓位管理 V3 - 固定金额 + 固定仓位数量
    - 开仓固定金额 open_size
    - 每次补仓固定金额 add_size
    - 最大仓位数量 max_positions (默认3: 1开仓+2补仓)
    """
    account_balance: float = 10000
    open_size: float = 2000
    add_size: float = 1000
    max_positions: int = 3
    max_single_loss: float = 0.02
    max_total_loss: float = 0.05
    max_drawdown: float = 0.15
    
    _position: Position = field(default_factory=Position)
    _peak_balance: float = 0.0
    _total_loss: float = 0.0
    
    def __post_init__(self):
        self._peak_balance = self.account_balance
    
    @property
    def position(self) -> Position:
        return self._position
    
    @property
    def current_balance(self) -> float:
        return self.account_balance - self._position.total_value
    
    @property
    def total_exposure(self) -> float:
        return self._position.total_value
    
    @property
    def loss(self) -> float:
        return self._peak_balance - self.account_balance
    
    @property
    def drawdown(self) -> float:
        if self._peak_balance == 0:
            return 0.0
        return self.loss / self._peak_balance
    
    def can_open(self, price: float) -> bool:
        if self._position.is_open:
            return False
        return self.current_balance >= self.open_size
    
    def can_add(self, price: float) -> bool:
        if not self._position.is_open:
            return False
        if self._position.count >= self.max_positions:
            return False
        return self.current_balance >= self.add_size
    
    def open_long(self, price: float, timestamp: int) -> Optional[Dict]:
        if not self.can_open(price):
            return None
        
        size = self.open_size / price
        self._position.side = PositionSide.LONG
        self._position.entries.append({
            'price': price,
            'size': size,
            'value': self.open_size,
            'timestamp': timestamp
        })
        self._position.total_value += self.open_size
        self._position.count = 1
        self._position.avg_price = self._calc_avg_price()
        
        return {
            'action': 'open_long',
            'price': price,
            'size': size,
            'value': self.open_size,
            'timestamp': timestamp
        }
    
    def add_long(self, price: float, timestamp: int) -> Optional[Dict]:
        if not self.can_add(price):
            return None
        
        size = self.add_size / price
        self._position.entries.append({
            'price': price,
            'size': size,
            'value': self.add_size,
            'timestamp': timestamp
        })
        self._position.total_value += self.add_size
        self._position.count += 1
        self._position.avg_price = self._calc_avg_price()
        
        return {
            'action': 'add_long',
            'price': price,
            'size': size,
            'value': self.add_size,
            'timestamp': timestamp
        }
    
    def close_long(self, price: float, timestamp: int) -> Optional[Dict]:
        if self._position.side != PositionSide.LONG:
            return None
        
        pnl = (price - self._position.avg_price) * sum(e['size'] for e in self._position.entries)
        
        self._position.side = PositionSide.FLAT
        self._position.entries.clear()
        self._position.total_value = 0
        self._position.avg_price = 0
        self._position.count = 0
        
        self.account_balance += pnl
        if self.account_balance > self._peak_balance:
            self._peak_balance = self.account_balance
        
        return {
            'action': 'close_long',
            'price': price,
            'pnl': pnl,
            'timestamp': timestamp
        }
    
    def _calc_avg_price(self) -> float:
        if not self._position.entries:
            return 0
        total_size = sum(e['size'] for e in self._position.entries)
        if total_size == 0:
            return 0
        return self._position.total_value / total_size
    
    def check_stop_loss(self, price: float) -> bool:
        if not self._position.is_open:
            return False
        if self._position.side == PositionSide.LONG:
            loss_pct = (self._position.avg_price - price) / self._position.avg_price
            return loss_pct >= self.max_single_loss
        return False
    
    def check_take_profit(self, price: float) -> bool:
        if not self._position.is_open:
            return False
        if self._position.side == PositionSide.LONG:
            profit_pct = (price - self._position.avg_price) / self._position.avg_price
            return profit_pct >= 2 * self.max_single_loss
        return False
    
    def reset(self):
        self._position = Position()
        self._peak_balance = self.account_balance
        self._total_loss = 0
