from typing import Dict, Tuple
import time
import pprint

# key: (base, notional_usd)
VAR_BATCH: Dict[Tuple[str, float], dict] = {}

def upsert_var_batch(quotes):
    """
    quotes: list[dict], each dict at least contains:
      base, notional_usd, bid, ask, mid, qty_used
    """
    now = time.time()

    print("\n[STATE] upsert_var_batch called")
    print(f"[STATE] incoming quotes count = {len(quotes)}")

    for i, q in enumerate(quotes):
        print(f"[STATE] quote[{i}] raw = {q}")

        base = str(q.get("base", "")).upper().strip()
        notional = float(q.get("notional_usd"))

        key = (base, notional)

        VAR_BATCH[key] = {
            **q,
            "ts": now,
        }

        print(f"[STATE] saved VAR_BATCH[{key}] =")
        pprint.pprint(VAR_BATCH[key])

    print("[STATE] VAR_BATCH keys now:")
    pprint.pprint(list(VAR_BATCH.keys()))
    print("")

def get_var(base: str, notional_usd: float):
    key = (base.upper().strip(), float(notional_usd))
    val = VAR_BATCH.get(key)

    print(f"[STATE] get_var lookup key = {key}")
    print(f"[STATE] get_var result = {val}")

    return val
