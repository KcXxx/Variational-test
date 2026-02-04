# backend/app/feeds/lighter_markets.py
import time
from typing import Any, Dict, List, Optional

import httpx

# 你之前用的这个 endpoint（有时 503，属于外部不稳定依赖）
DEFAULT_URL = "https://explorer.elliot.ai/api/markets"

_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
CACHE_TTL_SEC = 60  # 拉不到就别频繁打爆对方


async def fetch_lighter_markets(url: str = DEFAULT_URL) -> List[Dict[str, Any]]:
    """
    返回类似：
    [{"symbol":"BTC","market_index":1}, ...]
    若 upstream 503 / timeout，则返回 []（由 WS fallback 兜底）
    """
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < CACHE_TTL_SEC:
        return _CACHE["data"]

    timeout = httpx.Timeout(6.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            _CACHE["ts"] = now
            _CACHE["data"] = data
            return data
        except Exception:
            # upstream 不稳定：交给 WS fallback
            return []
