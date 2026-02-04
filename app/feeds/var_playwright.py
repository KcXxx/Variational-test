import asyncio
import os
import re
import time
from typing import Optional, List, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from app.config import settings
from app.state import STATE


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _extract_price(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text)
    if not m:
        return None
    return _to_float(m.group(1).replace(",", ""))


def _is_crypto_symbol(base: str) -> bool:
    # crude filter to drop obvious FX like USDCHF / USDCAD, etc.
    b = base.upper()
    if b.startswith("USD") and len(b) == 6:
        return False
    if b.endswith("USD") and len(b) == 6:
        return False
    return True


def _top_bases_from_lighter(limit: int) -> List[str]:
    """
    Choose candidates from STATE by:
      - has lighter volume
      - crypto-ish symbol
      - sort by volume desc
      - take top N
    """
    rows: List[Tuple[str, float]] = []
    for base, m in STATE.markets.items():
        if not _is_crypto_symbol(base):
            continue
        vol = getattr(m.stats, "daily_quote_volume", None)
        if vol is None:
            continue
        try:
            v = float(vol)
        except Exception:
            continue
        rows.append((base, v))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [b for b, _ in rows[:limit]]


class VarScraper:
    """
    Stable Var UI scraper:
      - single browser/context/page reused
      ️ - low frequency
      - supports debug one-off
    """

    def __init__(self):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        self.notional_usd = float(os.getenv("VAR_NOTIONAL_USD", "1500"))
        self.confirm_usd = float(os.getenv("VAR_CONFIRM_USD", "3000"))

        self.poll_interval_sec = float(os.getenv("VAR_POLL_INTERVAL_SEC", "8"))
        self.top_n = int(os.getenv("VAR_TOP_N", "50"))
        self.headless = os.getenv("VAR_HEADLESS", "1") != "0"

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._ctx = await self._browser.new_context()
        self._page = await self._ctx.new_page()
        print(f"[VAR] started headless={self.headless}, topN={self.top_n}, notional=${self.notional_usd}, confirm=${self.confirm_usd}")

    async def close(self):
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        print("[VAR] closed")

    async def _goto(self, base: str):
        assert self._page is not None
        url = settings.VAR_URL_TMPL.format(BASE=base)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    async def _try_set_size_usd(self, usd_value: float) -> bool:
        """
        Best-effort:
          - click '$' toggle if exists
          - find a likely input and type
        """
        assert self._page is not None
        p = self._page

        # try click "$"
        try:
            btn = p.get_by_role("button", name=re.compile(r"^\$$"))
            if await btn.count() > 0:
                await btn.first.click(timeout=1500)
                await asyncio.sleep(0.1)
        except Exception:
            pass

        # try find input
        size_input = None
        selectors = [
            'input[aria-label*="Size"]',
            'input[placeholder*="Size"]',
            'input[type="number"]',
            'input[type="text"]',
        ]
        for sel in selectors:
            try:
                loc = p.locator(sel)
                cnt = await loc.count()
                for i in range(min(cnt, 8)):
                    li = loc.nth(i)
                    if await li.is_visible():
                        size_input = li
                        break
                if size_input is not None:
                    break
            except Exception:
                continue

        if size_input is None:
            return False

        try:
            await size_input.click(timeout=2000)
            await p.keyboard.press("Meta+A")
            await p.keyboard.press("Backspace")
            await size_input.type(f"{usd_value}", delay=10)
            await asyncio.sleep(0.15)
            return True
        except Exception:
            return False

    async def _read_buy_price(self) -> Optional[float]:
        """
        Parse from Buy button text.
        """
        assert self._page is not None
        p = self._page

        try:
            btns = p.get_by_role("button", name=re.compile(r"Buy", re.IGNORECASE))
            n = await btns.count()
            for i in range(min(n, 8)):
                b = btns.nth(i)
                if not await b.is_visible():
                    continue
                txt = await b.inner_text()
                px = _extract_price(txt)
                if px is not None:
                    return px
        except Exception:
            pass

        # fallback: any text containing Buy
        try:
            loc = p.locator("text=/Buy/i")
            if await loc.count() > 0:
                txt = await loc.first.inner_text()
                px = _extract_price(txt)
                if px is not None:
                    return px
        except Exception:
            pass

        return None

    async def fetch_buy_once(self, base: str, usd_value: float) -> Tuple[Optional[float], str]:
        """
        One-shot fetch with an error string for debug.
        """
        try:
            await self._goto(base)
        except Exception as e:
            return None, f"goto_failed: {repr(e)}"

        ok = await self._try_set_size_usd(usd_value)
        if not ok:
            return None, "set_size_failed: cannot find/operate size input"

        px = await self._read_buy_price()
        if px is None:
            return None, "read_buy_failed: cannot find Buy price text/parse number"

        return px, "ok"

    async def debug_screenshot(self, path: str):
        try:
            assert self._page is not None
            await self._page.screenshot(path=path, full_page=True)
        except Exception:
            pass


async def var_poll_loop(stop_event: asyncio.Event):
    """
    Poll only Lighter TopN bases (and only those already in STATE.markets).
    This makes it much lighter than polling 131 markets.
    """
    scraper = VarScraper()
    await scraper.start()

    try:
        while not stop_event.is_set():
            bases = _top_bases_from_lighter(scraper.top_n)

            if not bases:
                await asyncio.sleep(1.0)
                continue

            for base in bases:
                if stop_event.is_set():
                    break

                m = STATE.markets.get(base)
                if not m:
                    continue

                px1500, reason1500 = await scraper.fetch_buy_once(base, scraper.notional_usd)
                if px1500 is not None:
                    m.var.buy_1500 = float(px1500)
                else:
                    m.var.buy_1500 = None

                px3000, reason3000 = await scraper.fetch_buy_once(base, scraper.confirm_usd)
                if px3000 is not None:
                    m.var.buy_confirm = float(px3000)
                else:
                    m.var.buy_confirm = None

                print(f"[VAR] {base} 1500={px1500} ({reason1500}) | 3000={px3000} ({reason3000})")

                # small spacing to avoid hammering
                await asyncio.sleep(0.4)

            await asyncio.sleep(scraper.poll_interval_sec)

    finally:
        await scraper.close()


# helper for debug endpoint
_async_debug_scraper: Optional[VarScraper] = None


async def var_debug_once(base: str, usd: float, take_screenshot: bool = True) -> dict:
    """
    Called by a debug API endpoint.
    Creates a fresh scraper each call (simple, reliable).
    """
    s = VarScraper()
    await s.start()
    try:
        px, reason = await s.fetch_buy_once(base, usd)
        shot_path = None
        if take_screenshot:
            shot_path = f"/tmp/var_debug_{base}_{int(usd)}_{int(time.time())}.png"
            await s.debug_screenshot(shot_path)
        return {"base": base, "usd": usd, "price": px, "reason": reason, "screenshot": shot_path}
    finally:
        await s.close()
