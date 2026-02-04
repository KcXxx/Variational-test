from typing import List, Optional
import math

def _quantile(sorted_vals: List[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    w = pos - lo
    return sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w

def median_iqr(vals: List[float]) -> (Optional[float], Optional[float]):
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if len(vals) < 20:
        return None, None
    s = sorted(vals)
    med = _quantile(s, 0.5)
    q25 = _quantile(s, 0.25)
    q75 = _quantile(s, 0.75)
    iqr = q75 - q25
    return med, iqr
