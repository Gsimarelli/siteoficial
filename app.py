"""
Dashboard — Painel de metricas dos bots Polymarket
Flask + Chart.js | Lê dados da Activity API (TRADE + REDEEM)
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FUNDER = os.getenv("POLY_FUNDER", "0xcd30786A7546807172050C6F4295F2CE292D943c")
DATA_API = "https://data-api.polymarket.com"
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)
log = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Cache (60s TTL)
# ---------------------------------------------------------------------------
_cache = {}
CACHE_TTL = 60


def cached_get(url, params=None, ttl=CACHE_TTL):
    key = f"{url}:{json.dumps(params or {}, sort_keys=True)}"
    now = time.time()
    if key in _cache and now - _cache[key]["t"] < ttl:
        return _cache[key]["data"]
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _cache[key] = {"data": data, "t": now}
        return data
    except Exception as e:
        log.error(f"API error: {e}")
        return _cache.get(key, {}).get("data", [])


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_all_activities():
    """Busca todas as atividades (TRADE + REDEEM) paginando."""
    all_acts = []
    offset = 0
    limit = 100
    while True:
        acts = cached_get(f"{DATA_API}/activity", {
            "user": FUNDER,
            "limit": str(limit),
            "offset": str(offset),
        }, ttl=120)
        if not acts or not isinstance(acts, list):
            break
        all_acts.extend(acts)
        if len(acts) < limit:
            break
        offset += limit
    return all_acts


def fetch_active_positions():
    """Posicoes ativas (aguardando resolucao)."""
    return cached_get(f"{DATA_API}/positions", {
        "user": FUNDER, "sizeThreshold": "0.01",
    }) or []


def classify_asset(title):
    t = (title or "").lower()
    if "ethereum" in t or "eth" in t:
        return "ETH"
    return "BTC"


def build_trades(activities):
    """
    Agrupa atividades por conditionId:
    - TRADE = compra (custo em USDC)
    - REDEEM = resgate (payout em USDC)
    PnL = redeem_total - trade_total
    """
    markets = defaultdict(lambda: {
        "trades": [],
        "redeems": [],
        "title": "",
        "slug": "",
        "asset": "BTC",
    })

    for a in activities:
        cid = a.get("conditionId", "")
        atype = a.get("type", "")
        title = a.get("title", "")

        if not cid:
            continue

        m = markets[cid]
        if title and not m["title"]:
            m["title"] = title
            m["asset"] = classify_asset(title)
            m["slug"] = a.get("slug", "")

        if atype == "TRADE":
            m["trades"].append({
                "usdc": float(a.get("usdcSize", 0)),
                "size": float(a.get("size", 0)),
                "price": float(a.get("price", 0)),
                "ts": int(a.get("timestamp", 0)),
                "side": a.get("side", ""),
                "outcome": a.get("outcome", ""),
            })
        elif atype == "REDEEM":
            m["redeems"].append({
                "usdc": float(a.get("usdcSize", 0)),
                "size": float(a.get("size", 0)),
                "ts": int(a.get("timestamp", 0)),
            })

    # Build trade list
    result = []
    for cid, m in markets.items():
        if not m["trades"]:
            continue

        cost = sum(t["usdc"] for t in m["trades"] if t["side"] == "BUY")
        sells = sum(t["usdc"] for t in m["trades"] if t["side"] == "SELL")
        redeem = sum(r["usdc"] for r in m["redeems"])
        payout = redeem + sells

        # Se nao tem redeem nem sell, posicao ainda aberta
        if not m["redeems"] and not sells:
            continue

        pnl = payout - cost
        won = pnl > 0

        # Timestamp da primeira compra
        first_ts = min(t["ts"] for t in m["trades"])
        ts = datetime.fromtimestamp(first_ts, tz=timezone.utc)

        # Outcome (side que apostou)
        outcome = m["trades"][0].get("outcome", "")
        avg_price = m["trades"][0].get("price", 0)

        result.append({
            "cid": cid[:16],
            "title": m["title"][:60],
            "asset": m["asset"],
            "outcome": outcome,
            "cost": round(cost, 2),
            "payout": round(payout, 2),
            "pnl": round(pnl, 2),
            "won": won,
            "avg_price": round(avg_price, 4),
            "ts": ts.isoformat(),
            "ts_dt": ts,
            "date": ts.strftime("%Y-%m-%d"),
        })

    result.sort(key=lambda x: x["ts_dt"], reverse=True)
    return result


def compute_metrics(trades, active_positions):
    """Calcula todas as metricas."""
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0

    # Per-asset
    assets = {}
    for name in ["BTC", "ETH"]:
        at = [t for t in trades if t["asset"] == name]
        aw = [t for t in at if t["won"]]
        al = [t for t in at if not t["won"]]
        assets[name] = {
            "wins": len(aw),
            "losses": len(al),
            "total": len(at),
            "wr": round(len(aw) / len(at) * 100, 1) if at else 0,
            "pnl": round(sum(t["pnl"] for t in at), 2),
            "avg_win": round(sum(t["pnl"] for t in aw) / len(aw), 2) if aw else 0,
            "avg_loss": round(sum(t["pnl"] for t in al) / len(al), 2) if al else 0,
        }

    # Daily PnL
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]

    daily_sorted = sorted(daily.items())
    cumulative = []
    running = 0
    for day, pnl in daily_sorted:
        running += pnl
        cumulative.append({
            "date": day,
            "pnl": round(pnl, 2),
            "cumulative": round(running, 2),
        })

    # Today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t["date"] == today]
    today_pnl = sum(t["pnl"] for t in today_trades)

    # Active
    active_value = sum(float(p.get("currentValue", 0) or 0) for p in active_positions)
    active_list = active_positions if isinstance(active_positions, list) else []

    return {
        "total": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "wr": round(wr, 1),
            "pnl": round(total_pnl, 2),
            "active_value": round(active_value, 2),
            "active_count": len(active_list),
        },
        "today": {
            "pnl": round(today_pnl, 2),
            "wins": len([t for t in today_trades if t["won"]]),
            "losses": len([t for t in today_trades if not t["won"]]),
            "trades": len(today_trades),
        },
        "assets": assets,
        "daily": cumulative,
        "recent": [
            {k: v for k, v in t.items() if k != "ts_dt"}
            for t in trades[:30]
        ],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    activities = fetch_all_activities()
    active = fetch_active_positions()
    trades = build_trades(activities)
    metrics = compute_metrics(trades, active)
    return jsonify(metrics)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info(f"Dashboard iniciado na porta {PORT}")
    log.info(f"Funder: {FUNDER}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
