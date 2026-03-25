"""
Dashboard — Painel simples dos bots Polymarket
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

FUNDER = os.getenv("POLY_FUNDER", "0xcd30786A7546807172050C6F4295F2CE292D943c")
DATA_API = "https://data-api.polymarket.com"
PORT = int(os.getenv("PORT", "8080"))

app = Flask(__name__)
log = logging.getLogger("dashboard")

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


def fetch_all_activities():
    all_acts = []
    offset = 0
    limit = 100
    while True:
        acts = cached_get(f"{DATA_API}/activity", {
            "user": FUNDER, "limit": str(limit), "offset": str(offset),
        }, ttl=120)
        if not acts or not isinstance(acts, list):
            break
        all_acts.extend(acts)
        if len(acts) < limit:
            break
        offset += limit
    return all_acts


def fetch_active_positions():
    return cached_get(f"{DATA_API}/positions", {
        "user": FUNDER, "sizeThreshold": "0.01",
    }) or []


def fetch_usdc_balance():
    """Busca saldo USDC on-chain do proxy wallet via Polygon RPC."""
    usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    # balanceOf(address) selector = 0x70a08231
    addr_padded = FUNDER.lower().replace("0x", "").zfill(64)
    data = "0x70a08231" + addr_padded
    try:
        resp = requests.post("https://polygon-bor-rpc.publicnode.com", json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": usdc, "data": data}, "latest"],
            "id": 1,
        }, timeout=10)
        result = resp.json().get("result", "0x0")
        # USDC has 6 decimals
        return int(result, 16) / 1e6
    except Exception:
        return 0.0


def classify_asset(title):
    t = (title or "").lower()
    return "ETH" if ("ethereum" in t or "eth-updown" in t) else "BTC"


def build_trades(activities):
    markets = defaultdict(lambda: {
        "trades": [], "redeems": [], "title": "", "slug": "", "asset": "BTC",
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
                "side": a.get("side", ""),
                "price": float(a.get("price", 0)),
                "ts": int(a.get("timestamp", 0)),
            })
        elif atype == "REDEEM":
            m["redeems"].append({
                "usdc": float(a.get("usdcSize", 0)),
                "ts": int(a.get("timestamp", 0)),
            })

    result = []
    for cid, m in markets.items():
        if not m["trades"]:
            continue

        cost = sum(t["usdc"] for t in m["trades"] if t["side"] == "BUY")
        sells = sum(t["usdc"] for t in m["trades"] if t["side"] == "SELL")
        redeem = sum(r["usdc"] for r in m["redeems"])
        payout = redeem + sells

        if not m["redeems"] and not sells:
            continue

        pnl = payout - cost
        first_ts = min(t["ts"] for t in m["trades"])
        ts = datetime.fromtimestamp(first_ts, tz=timezone.utc)

        result.append({
            "title": m["title"][:55],
            "asset": m["asset"],
            "cost": round(cost, 2),
            "payout": round(payout, 2),
            "pnl": round(pnl, 2),
            "won": pnl > 0,
            "ts": ts.isoformat(),
            "ts_dt": ts,
            "date": ts.strftime("%Y-%m-%d"),
        })

    result.sort(key=lambda x: x["ts_dt"], reverse=True)
    return result


def compute_metrics(trades, active_positions):
    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]

    # Saldo USDC real on-chain
    usdc_balance = fetch_usdc_balance()

    # Daily PnL
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    daily_sorted = sorted(daily.items())
    cumulative = []
    running = 0
    for day, pnl in daily_sorted:
        running += pnl
        cumulative.append({"date": day, "pnl": round(pnl, 2), "cumulative": round(running, 2)})

    # Today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t["date"] == today]
    today_pnl = sum(t["pnl"] for t in today_trades)

    return {
        "total_pnl": round(total_pnl, 2),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "usdc_balance": round(usdc_balance, 2),
        "today_pnl": round(today_pnl, 2),
        "today_trades": len(today_trades),
        "daily": cumulative,
        "recent": [
            {k: v for k, v in t.items() if k != "ts_dt"}
            for t in trades[:50]
        ],
    }


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
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=PORT, debug=False)
