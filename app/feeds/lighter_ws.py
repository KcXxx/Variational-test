# backend/app/feeds/lighter_ws.py
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import websockets

LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
SUB_PAYLOAD = {"type": "subscribe", "channel": "market_stats/all"}


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _pick_best_bid_ask(d: Dict[str, Any]) -> (Optional[float], Optional[float]):
    """
    Lighter 的 market_stats 里不同版本字段名可能略有差异。
    这里尽量“容错”地找 bid/ask。
    """
    # 常见字段候选
    bid_keys = ["best_bid", "bid", "bid_price", "bestBid", "bestBidPrice"]
    ask_keys = ["best_ask", "ask", "ask_price", "bestAsk", "bestAskPrice"]

    bid = None
    ask = None

    for k in bid_keys:
        if k in d and d[k] is not None:
            bid = _to_float(d[k])
            break
    for k in ask_keys:
        if k in d and d[k] is not None:
            ask = _to_float(d[k])
            break

    # 有些 payload 只给 mark/index，没有 bid/ask：那就退化用 mark_price
    if bid is None or ask is None:
        mp = _to_float(d.get("mark_price") or d.get("markPrice"))
        if bid is None:
            bid = mp
        if ask is None:
            ask = mp

    return bid, ask


@dataclass
class LighterWSState:
    """
    给 main.py / health / markets 接口读取的“共享状态”
    """
    session_id: Optional[str] = None
    last_error: Optional[str] = None
    last_message_ts: Optional[float] = None

    # 记录：market_id -> stats(dict)
    market_stats: Dict[int, Dict[str, Any]] = field(default_factory=dict)

    # 记录我们见过哪些 market_id（用于 debug）
    markets_seen: Set[int] = field(default_factory=set)

    # 内部锁，避免并发读写
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def set_error(self, err: Optional[str]) -> None:
        async with self._lock:
            self.last_error = err

    async def set_session(self, sid: Optional[str]) -> None:
        async with self._lock:
            self.session_id = sid

    async def update_market(self, market_id: int, stats: Dict[str, Any]) -> None:
        async with self._lock:
            self.market_stats[market_id] = stats
            self.markets_seen.add(market_id)
            self.last_message_ts = time.time()

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "session_id": self.session_id,
                "last_error": self.last_error,
                "last_message_ts": self.last_message_ts,
                "market_stats_keys": len(self.market_stats),
                "markets_seen": len(self.markets_seen),
            }


STATE = LighterWSState()


async def _handle_message(raw: str) -> None:
    """
    解析一条 WS 消息，把 market stats 写入 STATE
    """
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # 连接确认
    if isinstance(msg, dict) and msg.get("type") == "connected":
        await STATE.set_session(msg.get("session_id"))
        await STATE.set_error(None)
        return

    # 订阅推送（你前面测试脚本显示 channel: "market_stats:all"）
    if not isinstance(msg, dict):
        return

    channel = msg.get("channel") or ""
    if "market_stats" not in channel:
        return

    market_stats = msg.get("market_stats")
    if not isinstance(market_stats, dict):
        return

    # market_stats 结构： { "0": {...}, "48": {...}, ... }
    # key 可能是字符串 market_id
    for k, v in market_stats.items():
        if not isinstance(v, dict):
            continue

        # market_id 尽量从 v 里取，取不到就用 key
        mid = v.get("market_id")
        if mid is None:
            try:
                mid = int(k)
            except Exception:
                continue
        else:
            try:
                mid = int(mid)
            except Exception:
                continue

        symbol = v.get("symbol")
        index_price = _to_float(v.get("index_price") or v.get("indexPrice"))
        mark_price = _to_float(v.get("mark_price") or v.get("markPrice"))
        bid, ask = _pick_best_bid_ask(v)

        # 24h quote volume 有时不在 WS 推送里，这里若有就收；没有就留空
        vol = _to_float(
            v.get("daily_quote_volume")
            or v.get("dailyQuoteVolume")
            or v.get("quote_volume_24h")
            or v.get("quoteVolume24h")
        )

        out = {
            "symbol": symbol,
            "market_id": mid,
            "index_price": index_price,
            "mark_price": mark_price,
            "best_bid": bid,
            "best_ask": ask,
            "daily_quote_volume": vol,
        }
        await STATE.update_market(mid, out)


async def lighter_ws_loop(stop_event: asyncio.Event) -> None:
    """
    后端启动时创建任务：asyncio.create_task(lighter_ws_loop(stop_event))
    """
    backoff = 1.0
    await STATE.set_error(None)

    while not stop_event.is_set():
        try:
            async with websockets.connect(
                LIGHTER_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
            ) as ws:
                backoff = 1.0
                await ws.send(json.dumps(SUB_PAYLOAD))

                # 只要连接正常、持续收消息就会不断更新 STATE
                while not stop_event.is_set():
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="ignore")
                    await _handle_message(raw)

        except asyncio.CancelledError:
            break
        except Exception as e:
            await STATE.set_error(f"{type(e).__name__}: {e}")
            # 小退避，避免疯狂重连
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 15.0)

    # loop 要退出了
    await STATE.set_error(STATE.last_error or "stopped")


def start_lighter_ws(stop_event: asyncio.Event) -> asyncio.Task:
    """
    兼容你 main.py 里可能用的入口名：start_lighter_ws(...)
    """
    return asyncio.create_task(lighter_ws_loop(stop_event))
