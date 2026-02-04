import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from collections import deque

@dataclass
class BookTop:
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None

@dataclass
class MarketStats:
    daily_quote_volume: Optional[float] = None
    mark_price: Optional[float] = None

@dataclass
class VarQuote:
    buy_1500: Optional[float] = None
    ts_ms: int = 0

@dataclass
class ConfirmInfo:
    notional: float
    var_buy: Optional[float] = None
    lighter_vwap_ask: Optional[float] = None
    spread_bps_exec: Optional[float] = None
    spread_usd_exec: Optional[float] = None
    ts_ms: int = 0

@dataclass
class Computed:
    spread_usd: Optional[float] = None
    spread_bps: Optional[float] = None
    baseline_median_bps: Optional[float] = None
    baseline_iqr_bps: Optional[float] = None
    deviation_bps: Optional[float] = None
    anomaly: bool = False
    anomaly_started_s: Optional[int] = None
    last_confirm_s: Optional[int] = None
    confirm: Optional[ConfirmInfo] = None

@dataclass
class Market:
    market_id: int
    symbol: str
    base: str

    book: BookTop = field(default_factory=BookTop)
    stats: MarketStats = field(default_factory=MarketStats)
    var: VarQuote = field(default_factory=VarQuote)
    computed: Computed = field(default_factory=Computed)

    spread_bps_hist: deque = field(default_factory=lambda: deque(maxlen=7200))

class GlobalState:
    def __init__(self):
        self.markets: Dict[str, Market] = {}
        self.last_tick_ms: int = 0

STATE = GlobalState()

def now_ms() -> int:
    return int(time.time() * 1000)
