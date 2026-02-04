import os

class Settings:
    VAR_NOTIONAL_USD = float(os.getenv("VAR_NOTIONAL_USD", "1500"))
    CONFIRM_NOTIONAL_USD = float(os.getenv("CONFIRM_NOTIONAL_USD", "3000"))
    VAR_URL_TMPL = os.getenv("VAR_URL_TMPL", "https://omni.variational.io/perpetual/{BASE}")

    VAR_POLL_ALL_SEC = float(os.getenv("VAR_POLL_ALL_SEC", "10"))
    VAR_POLL_TOPN_SEC = float(os.getenv("VAR_POLL_TOPN_SEC", "4"))
    VAR_TOPN = int(os.getenv("VAR_TOPN", "15"))
    VAR_CONFIRM_COOLDOWN_SEC = float(os.getenv("VAR_CONFIRM_COOLDOWN_SEC", "30"))

    BASELINE_WINDOW_SEC = int(os.getenv("BASELINE_WINDOW_SEC", "1800"))
    PERSIST_SEC = int(os.getenv("PERSIST_SEC", "6"))
    MIN_DELTA_BPS = float(os.getenv("MIN_DELTA_BPS", "8"))

    LIGHTER_WS_URL = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    LIGHTER_MARKETS_URL = os.getenv("LIGHTER_MARKETS_URL", "https://explorer.elliot.ai/api/markets")

settings = Settings()
