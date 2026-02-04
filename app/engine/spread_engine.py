import asyncio
import time

from app.config import settings
from app.state import STATE, ConfirmInfo
from app.utils.stats import median_iqr

def _now_s() -> int:
    return int(time.time())

def _calc_bps(spread_usd: float, ref_price: float):
    if not ref_price or ref_price <= 0:
        return None
    return spread_usd / ref_price * 1e4

def _trim_hist(hist, window_sec: int):
    now = _now_s()
    while hist and (now - hist[0][0]) > window_sec:
        hist.popleft()

async def spread_engine_loop(stop_event: asyncio.Event):
    while not stop_event.is_set():
        now_s = _now_s()
        for base, m in STATE.markets.items():
            ask = m.book.best_ask
            var_buy = m.var.buy_1500
            if ask and var_buy:
                spread_usd = var_buy - ask
                spread_bps = _calc_bps(spread_usd, ask)
                m.computed.spread_usd = spread_usd
                m.computed.spread_bps = spread_bps

                if spread_bps is not None:
                    m.spread_bps_hist.append((now_s, spread_bps))
                    _trim_hist(m.spread_bps_hist, settings.BASELINE_WINDOW_SEC)

                    vals = [v for _, v in m.spread_bps_hist]
                    med, iqr = median_iqr(vals)
                    m.computed.baseline_median_bps = med
                    m.computed.baseline_iqr_bps = iqr

                    if med is not None and iqr is not None:
                        dev = spread_bps - med
                        m.computed.deviation_bps = dev
                        delta = max(settings.MIN_DELTA_BPS, 2.0 * iqr)
                        cond = abs(dev) >= delta

                        if cond:
                            if m.computed.anomaly_started_s is None:
                                m.computed.anomaly_started_s = now_s
                            if (now_s - m.computed.anomaly_started_s) >= settings.PERSIST_SEC:
                                m.computed.anomaly = True
                        else:
                            m.computed.anomaly = False
                            m.computed.anomaly_started_s = None
        await asyncio.sleep(1.0)

async def confirm_executor_loop(stop_event: asyncio.Event, var_get_quote_cb, lighter_vwap_cb):
    while not stop_event.is_set():
        now_s = _now_s()
        for base, m in STATE.markets.items():
            if not m.computed.anomaly:
                continue
            if m.computed.last_confirm_s and (now_s - m.computed.last_confirm_s) < settings.VAR_CONFIRM_COOLDOWN_SEC:
                continue
            try:
                var_buy = await var_get_quote_cb(base, settings.CONFIRM_NOTIONAL_USD)
                vwap = await lighter_vwap_cb(base, settings.CONFIRM_NOTIONAL_USD)
                if var_buy is None or vwap is None:
                    continue
                spread_usd = var_buy - vwap
                spread_bps = _calc_bps(spread_usd, vwap)
                m.computed.confirm = ConfirmInfo(
                    notional=settings.CONFIRM_NOTIONAL_USD,
                    var_buy=var_buy,
                    lighter_vwap_ask=vwap,
                    spread_usd_exec=spread_usd,
                    spread_bps_exec=spread_bps,
                    ts_ms=int(time.time()*1000),
                )
                m.computed.last_confirm_s = now_s
            except Exception:
                pass
        await asyncio.sleep(1.0)
