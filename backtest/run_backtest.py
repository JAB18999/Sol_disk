"""回测运行脚本"""
import sys
sys.path.insert(0, '/workspace/backtest')
import pandas as pd
import glob
from position_manager_v3 import PositionManagerV3
from stats_collector import StatsCollector
from engine import BacktestEngine
from strategy_ma_cross import create_strategy

def load_data(timeframe: str, data_dir: str = '/workspace/Sol_disk/DATA') -> pd.DataFrame:
    tf_map = {'5m': '5m', '15m': '15m', '30m': '30m', '1H': '1H', '2H': '2H'}
    tf = tf_map.get(timeframe, timeframe)
    file_path = f"{data_dir}/SOL-USDT-SWAP_{tf}_*.csv"
    files = glob.glob(file_path)
    if not files:
        raise FileNotFoundError(f"No data file found for {timeframe}")
    df = pd.read_csv(files[0])
    df['timestamp'] = df['timestamp_ms']
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'vol']]
    df.rename(columns={'vol': 'volume'}, inplace=True)
    return df

def run_backtest(timeframe: str, fast_period: int = 10, slow_period: int = 30,
                initial_balance: float = 10000, open_size: float = 2000, add_size: float = 1000):
    print(f"\n{'='*60}")
    print(f"时间周期: {timeframe}")
    print(f"{'='*60}")
    
    data = load_data(timeframe)
    print(f"数据范围: {len(data)} 根K线")
    
    pm = PositionManagerV3(
        account_balance=initial_balance,
        open_size=open_size,
        add_size=add_size,
        max_positions=3,
        max_single_loss=0.02,
        max_total_loss=0.05,
        max_drawdown=0.15
    )
    
    stats = StatsCollector()
    strategy = create_strategy(fast_period, slow_period)
    engine = BacktestEngine(data, pm, stats, strategy)
    result = engine.run()
    result.print_summary()
    return result

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeframe', '-t', default='1H')
    parser.add_argument('--fast', '-f', type=int, default=10)
    parser.add_argument('--slow', '-s', type=int, default=30)
    parser.add_argument('--balance', '-b', type=float, default=10000)
    args = parser.parse_args()
    
    run_backtest(args.timeframe, args.fast, args.slow, args.balance)
