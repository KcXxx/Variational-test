# -*- coding: utf-8 -*-
"""
SpreadWatch backend (FastAPI)

- Connects to Lighter WS (app.feeds.lighter_ws) to keep best_bid/best_ask/mark + daily_quote_volume
- Accepts Variational indicative quotes via /ingest/var_batch (from Tampermonkey)
- Computes spreads via /api/markets and stores time-series for chart (/chart)

Notes:
- We store VAR quotes keyed by (base, notional_int) to avoid float-key mismatch
- We do NOT filter negative spreads/bps; but we do ignore obviously broken quotes (bid/ask inverted or wildly off-mark)
"""
from __future__ import annotations

# ======== standard libs ========
import os
import time
import asyncio
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

# ======== third-party ========
import httpx  # (保留：你后续可能会用到)
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ======== lighter ws ========
from app.feeds import lighter_ws

APP_NAME = "SpreadWatch"

# --------------------------
# FastAPI app  (⚠️必须在任何 @app.xxx 之前创建)
# --------------------------
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# Sampling config (chart)
# --------------------------
SAMPLE_EVERY_S = float(os.getenv("SPREAD_SAMPLE_EVERY_S", "2.0"))  # 2 seconds
MAX_WINDOW_S = 2 * 60 * 60  # keep 2 hours in memory
MAX_POINTS = int(MAX_WINDOW_S / max(SAMPLE_EVERY_S, 0.25)) + 50

# --------------------------
# In-memory states
# --------------------------
BOOT_TS = time.time()

# Lighter state object (from lighter_ws)
LIGHTER_STATE = None  # set on startup

# VAR quotes keyed by (BASE, notional_int)
# value: {"ts_ms": int, "bid": float, "ask": float, "mid": Optional[float], "qty_used": Optional[float]}
VAR_BATCH: Dict[Tuple[str, int], Dict[str, Any]] = {}

# For quick base listing (what the ingest is currently sending)
ACTIVE_BASES: Set[str] = set()

# Chart series:
# key: (base, mode)  value: deque[(t_ms, spread_usd, spread_bps)]
SPREAD_SERIES: Dict[Tuple[str, str], deque] = {}

# Lighter WS lifecycle
_stop_event: Optional[asyncio.Event] = None
_lighter_task: Optional[asyncio.Task] = None


# --------------------------
# Helpers
# --------------------------
def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _notional_key(notional: Any) -> int:
    """
    Use int key to avoid float mismatch (1500 vs 1500.0).
    We round to nearest int.
    """
    f = _to_f(notional)
    if f is None:
        return 0
    return int(round(f))


def _get_ws_market_stats() -> Dict[int, Dict[str, Any]]:
    """
    Pull market_stats dict from the lighter_ws STATE object.
    """
    global LIGHTER_STATE
    st = LIGHTER_STATE
    if st is None:
        return {}
    ms = getattr(st, "market_stats", None)
    if isinstance(ms, dict):
        return ms
    return {}


def _pick_ws_rows(top_n: int = 20) -> List[Dict[str, Any]]:
    """
    Return rows from Lighter WS state. Sort by daily_quote_volume desc when available,
    otherwise by market_id.
    Each row: {"base","lighter_market_id","lighter_best_bid","lighter_best_ask","lighter_mark_price","lighter_daily_quote_volume"}
    """
    ms = _get_ws_market_stats()
    rows: List[Dict[str, Any]] = []
    for k, v in ms.items():
        if not isinstance(v, dict):
            continue
        base = str(v.get("symbol") or "").upper().strip()
        if not base:
            continue
        rows.append(
            {
                "base": base,
                "lighter_market_id": int(v.get("market_id", k))
                if _to_f(v.get("market_id", k)) is not None
                else int(k),
                "lighter_best_bid": _to_f(v.get("best_bid")),
                "lighter_best_ask": _to_f(v.get("best_ask")),
                "lighter_mark_price": _to_f(v.get("mark_price") or v.get("index_price")),
                "lighter_daily_quote_volume": _to_f(v.get("daily_quote_volume")),
            }
        )

    # sort key: volume desc (None last), then market_id
    def sk(r: Dict[str, Any]):
        vol = r.get("lighter_daily_quote_volume")
        vol_key = vol if isinstance(vol, (int, float)) else -1.0
        return (-vol_key, r.get("lighter_market_id", 0))

    rows.sort(key=sk)
    return rows[: max(0, min(int(top_n), 200))]


def _get_lighter_price(base: str) -> Dict[str, Optional[float]]:
    """
    Find lighter best_bid/ask/mark for base.
    """
    base = (base or "").upper().strip()
    ms = _get_ws_market_stats()
    for v in ms.values():
        if isinstance(v, dict) and str(v.get("symbol") or "").upper().strip() == base:
            return {
                "bid": _to_f(v.get("best_bid")),
                "ask": _to_f(v.get("best_ask")),
                "mark": _to_f(v.get("mark_price") or v.get("index_price")),
            }
    return {"bid": None, "ask": None, "mark": None}


def _get_var_quote(base: str, notional_int: int) -> Optional[Dict[str, Any]]:
    """
    Exact match by (base, notional_int), else fallback to nearest notional for same base.
    """
    base = (base or "").upper().strip()
    if not base:
        return None

    exact = VAR_BATCH.get((base, notional_int))
    if exact:
        return exact

    # fallback: find closest notional for same base
    best = None
    best_d = None
    for (b, n), q in VAR_BATCH.items():
        if b != base:
            continue
        d = abs(int(n) - int(notional_int))
        if best is None or d < best_d:
            best = q
            best_d = d
    return best


def _compute_spread(base: str, notional: float, mode: str) -> Dict[str, Any]:
    """
    mode:
      - lighter_bid_minus_var_sell : lighter_bid - var_sell_price (var_bid)
      - lighter_ask_minus_var_buy  : lighter_ask - var_buy_price (var_ask)
      - lighter_mid_minus_var_mid  : lighter_mid - var_mid (fallback to (bid+ask)/2)
    """
    base_u = (base or "").upper().strip()
    mode = (mode or "lighter_bid_minus_var_sell").strip()

    lp = _get_lighter_price(base_u)
    lighter_bid = lp["bid"]
    lighter_ask = lp["ask"]
    lighter_mid = None
    if lighter_bid is not None and lighter_ask is not None:
        lighter_mid = (lighter_bid + lighter_ask) / 2.0
    else:
        lighter_mid = lp["mark"]

    n_key = _notional_key(notional)
    vq = _get_var_quote(base_u, n_key)
    var_bid = var_ask = var_mid = None
    if vq:
        var_bid = _to_f(vq.get("bid"))
        var_ask = _to_f(vq.get("ask"))
        var_mid = _to_f(vq.get("mid"))
        if var_mid is None and var_bid is not None and var_ask is not None:
            var_mid = (var_bid + var_ask) / 2.0

    spread_usd = spread_bps = None
    ref = None

    if mode == "lighter_bid_minus_var_sell":
        if lighter_bid is not None and var_bid is not None:
            spread_usd = float(lighter_bid - var_bid)
            ref = float(var_bid) if var_bid else None
    elif mode == "lighter_ask_minus_var_buy":
        if lighter_ask is not None and var_ask is not None:
            spread_usd = float(lighter_ask - var_ask)
            ref = float(var_ask) if var_ask else None
    else:  # mid-mid
        if lighter_mid is not None and var_mid is not None:
            spread_usd = float(lighter_mid - var_mid)
            ref = float(var_mid) if var_mid else None

    if spread_usd is not None and ref not in (None, 0.0):
        spread_bps = float(spread_usd / ref * 10000.0)

    return {
        "base": base_u,
        "notional": float(notional),
        "mode": mode,
        "lighter_bid": lighter_bid,
        "lighter_ask": lighter_ask,
        "lighter_mid": lighter_mid,
        "var_bid": var_bid,
        "var_ask": var_ask,
        "var_mid": var_mid,
        "spread_usd": spread_usd,
        "spread_bps": spread_bps,
    }


def _push_point(base: str, mode: str, spread_usd: Optional[float], spread_bps: Optional[float]) -> None:
    if spread_usd is None or spread_bps is None:
        return
    base = (base or "").upper().strip()
    if not base:
        return
    mode = (mode or "").strip() or "lighter_bid_minus_var_sell"

    key = (base, mode)
    dq = SPREAD_SERIES.get(key)
    if dq is None:
        dq = deque(maxlen=MAX_POINTS)
        SPREAD_SERIES[key] = dq
    dq.append((_now_ms(), float(spread_usd), float(spread_bps)))


def _parse_window_s(window: str) -> int:
    """
    window: "5m" / "30m" / "2h" (default 5m)
    """
    w = (window or "5m").strip().lower()
    if w.endswith("m"):
        return int(float(w[:-1]) * 60)
    if w.endswith("h"):
        return int(float(w[:-1]) * 3600)
    return int(float(w))


# --------------------------
# Extra pages
# --------------------------
@app.get("/chart2", response_class=HTMLResponse)
def chart2():
    p = Path(__file__).parent / "static" / "chart_v2.html"
    if not p.exists():
        return HTMLResponse("<h3>chart_v2.html not found</h3>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


# --------------------------
# Lifecycle
# --------------------------
@app.on_event("startup")
async def _startup() -> None:
    """
    Start lighter ws in background.
    lighter_ws.start_lighter_ws(stop_event) requires a stop_event in your version.
    """
    global LIGHTER_STATE, _stop_event, _lighter_task

    _stop_event = asyncio.Event()
    LIGHTER_STATE = getattr(lighter_ws, "STATE", None)

    starter = getattr(lighter_ws, "start_lighter_ws", None) or getattr(lighter_ws, "start", None)
    if starter is None:
        print("[BOOT] WARN: lighter_ws has no start_lighter_ws/start")
        return

    async def runner():
        try:
            # try passing stop_event, fallback to no-arg
            try:
                await starter(_stop_event)  # type: ignore
            except TypeError:
                await starter()  # type: ignore
        except Exception as e:
            print(f"[BOOT] ERR: start_lighter_ws failed: {type(e).__name__}: {e}")

    _lighter_task = asyncio.create_task(runner())
    print("[BOOT] OK: start_lighter_ws task created")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _stop_event, _lighter_task
    try:
        if _stop_event is not None:
            _stop_event.set()
        if _lighter_task is not None:
            _lighter_task.cancel()
    except Exception:
        pass


# --------------------------
# APIs
# --------------------------
@app.get("/health")
async def health() -> Dict[str, Any]:
    ms = _get_ws_market_stats()
    return {
        "ok": True,
        "tick_ms": _now_ms(),
        "markets": len(ms),
        "lighter_session_id": getattr(LIGHTER_STATE, "session_id", None) if LIGHTER_STATE else None,
        "lighter_last_error": getattr(LIGHTER_STATE, "last_error", None) if LIGHTER_STATE else None,
        "uptime_sec": int(time.time() - BOOT_TS),
        "active_bases": sorted(ACTIVE_BASES),
        "var_batch_keys": len(VAR_BATCH),
        "sample_every_s": SAMPLE_EVERY_S,
    }


@app.get("/api/top_bases")
async def top_bases(top_n: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    rows = _pick_ws_rows(top_n=int(top_n))
    return {"ok": True, "top_n": int(top_n), "count": len(rows), "rows": rows}


@app.get("/api/chart/bases")
async def chart_bases() -> Dict[str, Any]:
    bases = sorted(ACTIVE_BASES)
    return {"ok": True, "count": len(bases), "bases": bases}


@app.post("/ingest/var_batch")
async def ingest_var_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Expected payload:
      { "quotes": [ { base, notional_usd, bid, ask, mid?, qty_used? }, ... ] }
    """
    quotes = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(quotes, list):
        return {"ok": True, "count": 0}

    stored = 0
    for q in quotes:
        if not isinstance(q, dict):
            continue
        base = str(q.get("base") or "").upper().strip()
        if not base:
            continue

        notional_int = _notional_key(q.get("notional_usd") or q.get("notional") or 0)
        bid = _to_f(q.get("bid"))
        ask = _to_f(q.get("ask"))
        mid = _to_f(q.get("mid"))
        qty_used = _to_f(q.get("qty_used"))

        # If bid/ask inverted, swap
        if bid is not None and ask is not None and bid > ask:
            bid, ask = ask, bid

        # Basic sanity: require bid/ask
        if bid is None or ask is None:
            continue

        # Extra sanity: if wildly off the lighter mark (>5%), ignore this quote
        lp = _get_lighter_price(base)
        ref = lp.get("mark") or lp.get("bid") or lp.get("ask")
        if ref and abs((ask + bid) / 2.0 - float(ref)) / float(ref) > 0.05:
            ACTIVE_BASES.add(base)
            continue

        VAR_BATCH[(base, notional_int)] = {
            "ts_ms": _now_ms(),
            "base": base,
            "notional_int": notional_int,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "qty_used": qty_used,
        }
        ACTIVE_BASES.add(base)
        stored += 1

        # push points for chart
        for mode in ("lighter_bid_minus_var_sell", "lighter_ask_minus_var_buy"):
            s = _compute_spread(base=base, notional=float(notional_int), mode=mode)
            _push_point(base=base, mode=mode, spread_usd=s["spread_usd"], spread_bps=s["spread_bps"])

    return {"ok": True, "count": stored}


@app.get("/api/markets")
async def markets(
    notional: float = Query(1500.0, gt=0),
    bases: str = Query(""),
    spread_mode: str = Query("lighter_bid_minus_var_sell"),
) -> List[Dict[str, Any]]:
    want = [b.strip().upper() for b in (bases or "").split(",") if b.strip()]
    out: List[Dict[str, Any]] = []
    for base in want:
        s = _compute_spread(base=base, notional=float(notional), mode=spread_mode)
        out.append(
            {
                "base": base,
                "notional": float(notional),
                "mode": spread_mode,
                "lighter_bid": s["lighter_bid"],
                "lighter_ask": s["lighter_ask"],
                "var_bid": s["var_bid"],
                "var_ask": s["var_ask"],
                "spread_usd": s["spread_usd"],
                "spread_bps": s["spread_bps"],
            }
        )
    return out


@app.get("/api/debug/latest")
async def debug_latest(
    base: str = Query("BTC"),
    notional: float = Query(1500.0, gt=0),
    mode: str = Query("lighter_bid_minus_var_sell"),
) -> Dict[str, Any]:
    s = _compute_spread(base=base, notional=float(notional), mode=mode)
    return {"ok": True, **s}


@app.get("/api/chart/series")
async def chart_series(
    base: str = Query("BTC"),
    window: str = Query("5m"),
    metric: str = Query("bps"),
    mode: str = Query("lighter_bid_minus_var_sell"),
) -> Dict[str, Any]:
    base = (base or "").upper().strip()
    metric = (metric or "bps").lower().strip()
    mode = (mode or "lighter_bid_minus_var_sell").strip()

    win_s = _parse_window_s(window)
    cutoff_ms = _now_ms() - win_s * 1000

    dq = SPREAD_SERIES.get((base, mode))
    points: List[Dict[str, Any]] = []
    latest = None

    if dq:
        for t_ms, usd, bps in dq:
            if t_ms < cutoff_ms:
                continue
            v = bps if metric == "bps" else usd
            points.append({"t": int(t_ms), "v": float(v)})
        if points:
            latest = points[-1]

    return {
        "ok": True,
        "base": base,
        "metric": metric,
        "mode": mode,
        "window": window,
        "count": len(points),
        "points": points,
        "latest": latest,
    }


# --------------------------
# Front-end (single HTML, no build)
# --------------------------
CHART_HTML = r"""
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>SpreadWatch</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.umd.min.js"></script>
  <style>
    :root{
      --bg:#0b1220; --panel:#0f1a2b; --panel2:#0c1626;
      --text:#e5e7eb; --muted:#93a4b8; --border:rgba(255,255,255,.08);
    }
    html,body{height:100%;}
    body{
      margin:0; background:linear-gradient(180deg,var(--bg),#070c16);
      color:var(--text); font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;
      display:flex; justify-content:center;
    }
    .wrap{width:min(1100px, calc(100% - 24px)); margin:20px 0 32px;}
    h1{font-size:20px; margin:0 0 10px;}
    .sub{color:var(--muted); font-size:13px; margin-bottom:14px;}
    .bar{
      background:linear-gradient(180deg,var(--panel),var(--panel2));
      border:1px solid var(--border); border-radius:14px;
      padding:12px; display:flex; gap:10px; flex-wrap:wrap; align-items:center;
      box-shadow: 0 18px 40px rgba(0,0,0,.35);
    }
    label{font-size:12px; color:var(--muted); margin-right:6px;}
    select,button{
      background:#0a1322; color:var(--text);
      border:1px solid var(--border); border-radius:10px;
      padding:8px 10px; font-size:13px;
      outline:none;
    }
    button{cursor:pointer;}
    button.primary{background:rgba(59,130,246,.14); border-color:rgba(59,130,246,.35);}
    button.ghost{background:transparent;}
    .spacer{flex:1;}
    .meta{color:var(--muted); font-size:12px;}
    .card{
      margin-top:12px;
      background:linear-gradient(180deg,var(--panel),var(--panel2));
      border:1px solid var(--border); border-radius:14px;
      padding:12px; box-shadow: 0 18px 40px rgba(0,0,0,.35);
    }
    .chartbox{position:relative; height:420px;}
    canvas{position:absolute; inset:0;}
    .hint{margin-top:8px; color:var(--muted); font-size:12px;}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>SpreadWatch（单币种）</h1>
    <div class="sub">每 2 秒刷新一次；支持 bps / USD；不过滤负数。鼠标悬浮可查看具体值；滚轮缩放、拖拽平移。</div>

    <div class="bar">
      <div>
        <label>币种</label>
        <select id="base"></select>
      </div>

      <div>
        <label>时间窗口</label>
        <select id="window">
          <option value="5m">最近 5 分钟</option>
          <option value="30m" selected>最近 30 分钟</option>
          <option value="2h">最近 2 小时</option>
        </select>
      </div>

      <div>
        <label>指标</label>
        <select id="metric">
          <option value="bps" selected>bps</option>
          <option value="usd">USD</option>
        </select>
      </div>

      <div>
        <label>模式</label>
        <select id="mode">
          <option value="lighter_bid_minus_var_sell" selected>lighter_bid - var_sell</option>
          <option value="lighter_ask_minus_var_buy">lighter_ask - var_buy</option>
          <option value="lighter_mid_minus_var_mid">lighter_mid - var_mid</option>
        </select>
      </div>

      <button class="primary" id="apply">应用</button>
      <button class="ghost" id="resetZoom">重置缩放</button>

      <div class="spacer"></div>
      <div class="meta" id="status">loading…</div>
    </div>

    <div class="card">
      <div class="chartbox">
        <canvas id="c"></canvas>
      </div>
      <div class="hint">提示：如果曲线一直空，先检查 Tampermonkey 是否在持续 POST /ingest/var_batch；再打开 /api/debug/latest 看 var_bid/ask 是否为 null。</div>
    </div>
  </div>

<script>
const $ = (id) => document.getElementById(id);

let cfg = { base:"BTC", window:"30m", metric:"bps", mode:"lighter_bid_minus_var_sell" };

async function jget(url){
  const r = await fetch(url, {cache:"no-store"});
  return await r.json();
}

function fmt(v){
  if (v === null || v === undefined) return "null";
  if (Math.abs(v) >= 1000) return v.toFixed(2);
  if (Math.abs(v) >= 100) return v.toFixed(3);
  if (Math.abs(v) >= 10) return v.toFixed(4);
  return v.toFixed(6);
}

const ctx = $("c").getContext("2d");
const chart = new Chart(ctx, {
  type: "line",
  data: { datasets: [{ label:"spread", data:[], parsing:false, pointRadius:0, borderWidth:2, tension:0.2 }]},
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: {mode:"nearest", intersect:false},
    scales: {
      x: { type:"time", time:{tooltipFormat:"HH:mm:ss"}, ticks:{color:"#93a4b8"}, grid:{color:"rgba(255,255,255,.06)"} },
      y: { ticks:{color:"#93a4b8"}, grid:{color:"rgba(255,255,255,.06)"} }
    },
    plugins: {
      legend: {display:false},
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const v = ctx.raw && ctx.raw.y;
            const unit = (cfg.metric === "bps") ? " bps" : " USD";
            return " " + fmt(v) + unit;
          }
        }
      },
      zoom: {
        zoom: { wheel:{enabled:true}, pinch:{enabled:true}, mode:"x" },
        pan: { enabled:true, mode:"x", modifierKey:null }
      }
    }
  }
});

async function loadBases(){
  const j = await jget("/api/chart/bases");
  const bases = (j && j.bases) ? j.bases : [];
  const sel = $("base");
  sel.innerHTML = "";
  for (const b of bases){
    const opt = document.createElement("option");
    opt.value = b; opt.textContent = b;
    sel.appendChild(opt);
  }
  if (bases.includes(cfg.base)) sel.value = cfg.base;
  else if (bases.length) { cfg.base = bases[0]; sel.value = cfg.base; }
}

async function loadSeries(){
  const url = `/api/chart/series?base=${encodeURIComponent(cfg.base)}&window=${encodeURIComponent(cfg.window)}&metric=${encodeURIComponent(cfg.metric)}&mode=${encodeURIComponent(cfg.mode)}`;
  const j = await jget(url);
  const pts = (j && j.points) ? j.points : [];
  chart.data.datasets[0].data = pts.map(p => ({x:p.t, y:p.v}));
  chart.update("none");

  const latest = j.latest ? j.latest.v : null;
  $("status").textContent =
    `${cfg.base} | ${cfg.metric} | ${cfg.window} | ${cfg.mode} | points=${pts.length}` +
    (latest!==null ? ` | latest=${fmt(latest)}` : "");
}

function applyFromUI(){
  cfg.base = $("base").value;
  cfg.window = $("window").value;
  cfg.metric = $("metric").value;
  cfg.mode = $("mode").value;
}

$("apply").addEventListener("click", async () => { applyFromUI(); await loadSeries(); });
$("resetZoom").addEventListener("click", () => chart.resetZoom());

async function main(){
  await loadBases();
  $("window").value = cfg.window;
  $("metric").value = cfg.metric;
  $("mode").value = cfg.mode;

  await loadSeries();
  setInterval(loadSeries, 2000);
  setInterval(loadBases, 5000);
}
main().catch(e => $("status").textContent = "ERR: " + String(e));
</script>
</body>
</html>
"""

@app.get("/chart", response_class=HTMLResponse)
async def chart_page() -> HTMLResponse:
    return HTMLResponse(CHART_HTML)
