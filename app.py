"""
Dashboard V2 — Painel completo dos bots Polymarket
Flask + Chart.js | Filtros, heatmap, streaks, ROI
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FUNDER = os.getenv("POLY_FUNDER", "0xcd30786A7546807172050C6F4295F2CE292D943c")
DATA_API = "https://data-api.polymarket.com"
PORT = int(os.getenv("PORT", "8080"))

# Bot detection: bot trades are $5-$13 in updown-5m markets
BOT_MIN_COST = 4.0
BOT_MAX_COST = 14.0

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


def classify_asset(title):
    t = (title or "").lower()
    return "ETH" if ("ethereum" in t or "eth-updown" in t) else "BTC"


def is_bot_trade(trade):
    """Detecta se é trade do bot (updown-5m, custo $5-$13)."""
    slug = trade.get("slug", "")
    cost = trade.get("cost", 0)
    is_updown = "updown-5m" in slug
    is_bot_range = BOT_MIN_COST <= cost <= BOT_MAX_COST
    return is_updown and is_bot_range


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
        won = pnl > 0
        first_ts = min(t["ts"] for t in m["trades"])
        ts = datetime.fromtimestamp(first_ts, tz=timezone.utc)
        outcome = m["trades"][0].get("outcome", "")
        avg_price = m["trades"][0].get("price", 0)

        trade = {
            "cid": cid[:16],
            "title": m["title"][:60],
            "slug": m["slug"],
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
            "hour": ts.hour,
        }
        trade["is_bot"] = is_bot_trade(trade)
        result.append(trade)

    result.sort(key=lambda x: x["ts_dt"], reverse=True)
    return result


def compute_streak(trades):
    """Calcula streak atual e melhor streak."""
    if not trades:
        return {"current": 0, "current_type": "none", "best_win": 0, "best_loss": 0}

    sorted_trades = sorted(trades, key=lambda x: x["ts_dt"])

    # Current streak
    current = 0
    current_type = "win" if sorted_trades[-1]["won"] else "loss"
    for t in reversed(sorted_trades):
        if t["won"] == (current_type == "win"):
            current += 1
        else:
            break

    # Best streaks
    best_win = 0
    best_loss = 0
    streak = 0
    last_won = None
    for t in sorted_trades:
        if t["won"] == last_won:
            streak += 1
        else:
            streak = 1
            last_won = t["won"]
        if t["won"]:
            best_win = max(best_win, streak)
        else:
            best_loss = max(best_loss, streak)

    return {
        "current": current,
        "current_type": current_type,
        "best_win": best_win,
        "best_loss": best_loss,
    }


def compute_heatmap(trades):
    """Mapa de calor: WR por hora UTC."""
    hours = {}
    for h in range(24):
        ht = [t for t in trades if t["hour"] == h]
        wins = len([t for t in ht if t["won"]])
        total = len(ht)
        hours[h] = {
            "wins": wins,
            "losses": total - wins,
            "total": total,
            "wr": round(wins / total * 100, 1) if total > 0 else 0,
            "pnl": round(sum(t["pnl"] for t in ht), 2),
        }
    return hours


def compute_metrics(trades, active_positions):
    all_trades = trades
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["cost"] for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0
    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    # Bot-only trades
    bot_trades = [t for t in trades if t["is_bot"]]
    bot_wins = [t for t in bot_trades if t["won"]]
    bot_losses = [t for t in bot_trades if not t["won"]]
    bot_pnl = sum(t["pnl"] for t in bot_trades)
    bot_cost = sum(t["cost"] for t in bot_trades)
    bot_wr = len(bot_wins) / len(bot_trades) * 100 if bot_trades else 0
    bot_roi = (bot_pnl / bot_cost * 100) if bot_cost > 0 else 0

    # Per-asset
    assets = {}
    for name in ["BTC", "ETH"]:
        at = [t for t in trades if t["asset"] == name]
        aw = [t for t in at if t["won"]]
        al = [t for t in at if not t["won"]]
        ac = sum(t["cost"] for t in at)
        ap = sum(t["pnl"] for t in at)
        # Bot only per asset
        abot = [t for t in at if t["is_bot"]]
        abw = [t for t in abot if t["won"]]
        abl = [t for t in abot if not t["won"]]
        assets[name] = {
            "wins": len(aw), "losses": len(al), "total": len(at),
            "wr": round(len(aw) / len(at) * 100, 1) if at else 0,
            "pnl": round(ap, 2),
            "roi": round((ap / ac * 100) if ac > 0 else 0, 1),
            "avg_win": round(sum(t["pnl"] for t in aw) / len(aw), 2) if aw else 0,
            "avg_loss": round(sum(t["pnl"] for t in al) / len(al), 2) if al else 0,
            "bot_wins": len(abw), "bot_losses": len(abl), "bot_total": len(abot),
            "bot_wr": round(len(abw) / len(abot) * 100, 1) if abot else 0,
            "bot_pnl": round(sum(t["pnl"] for t in abot), 2),
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
        cumulative.append({"date": day, "pnl": round(pnl, 2), "cumulative": round(running, 2)})

    # Today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if t["date"] == today]
    today_pnl = sum(t["pnl"] for t in today_trades)

    # Periods
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    week_trades = [t for t in trades if t["date"] >= week_ago]
    month_trades = [t for t in trades if t["date"] >= month_ago]

    # Active
    active_value = sum(float(p.get("currentValue", 0) or 0) for p in active_positions)
    active_list = active_positions if isinstance(active_positions, list) else []

    # Streak
    streak = compute_streak(trades)

    # Heatmap
    heatmap = compute_heatmap(trades)

    return {
        "total": {
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "wr": round(wr, 1), "pnl": round(total_pnl, 2),
            "roi": round(roi, 2), "invested": round(total_cost, 2),
            "active_value": round(active_value, 2), "active_count": len(active_list),
        },
        "bot": {
            "trades": len(bot_trades), "wins": len(bot_wins), "losses": len(bot_losses),
            "wr": round(bot_wr, 1), "pnl": round(bot_pnl, 2),
            "roi": round(bot_roi, 2), "invested": round(bot_cost, 2),
        },
        "today": {
            "pnl": round(today_pnl, 2),
            "wins": len([t for t in today_trades if t["won"]]),
            "losses": len([t for t in today_trades if not t["won"]]),
            "trades": len(today_trades),
        },
        "week": {
            "pnl": round(sum(t["pnl"] for t in week_trades), 2),
            "trades": len(week_trades),
            "wins": len([t for t in week_trades if t["won"]]),
            "losses": len([t for t in week_trades if not t["won"]]),
        },
        "month": {
            "pnl": round(sum(t["pnl"] for t in month_trades), 2),
            "trades": len(month_trades),
            "wins": len([t for t in month_trades if t["won"]]),
            "losses": len([t for t in month_trades if not t["won"]]),
        },
        "assets": assets,
        "daily": cumulative,
        "streak": streak,
        "heatmap": heatmap,
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
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info(f"Dashboard V2 iniciado na porta {PORT}")
    log.info(f"Funder: {FUNDER}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
