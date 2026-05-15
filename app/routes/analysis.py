import time
from threading import Lock

from flask import Blueprint, current_app, jsonify

bp = Blueprint("analysis", __name__)

# Simple in-memory sparkline cache: ticker → {data, expires_at}
_sparkline_cache: dict = {}
_sparkline_lock = Lock()
_SPARKLINE_TTL = 3600  # seconds


@bp.post("/api/run")
def trigger_run():
    analyzer = current_app.extensions["analyzer"]
    portfolio = current_app.extensions["portfolio"]

    if analyzer.status["status"] == "running":
        return jsonify({"error": "Analysis already running"}), 409

    holdings = portfolio.get_holdings()
    if not holdings:
        return jsonify({"error": "No holdings found. Add stocks.xlsx or sync T212."}), 400

    analyzer.run_in_background(holdings)
    return jsonify({"status": "started", "ticker_count": len(holdings)}), 202


@bp.get("/api/status")
def get_status():
    analyzer = current_app.extensions["analyzer"]
    return jsonify(analyzer.status)


@bp.get("/api/dashboard")
def dashboard_data():
    db = current_app.extensions["db"]
    analyses = db.get_latest_analyses()
    positions = db.get_positions()
    pos_map = {p["ticker"]: p for p in positions}

    cards = []
    total_value = 0.0
    total_cost = 0.0

    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        cost   = a.get("cost_basis") or p.get("avg_cost") or 0
        shares = a.get("shares")     or p.get("shares")   or 0
        price  = a.get("current_price") or 0
        total_value += price * shares
        total_cost  += cost * shares

        handoff = db.get_handoff_note(a["ticker"])
        cards.append({**a, "handoff_note": handoff})

    summary = {
        "total_value":   round(total_value, 2),
        "total_cost":    round(total_cost, 2),
        "total_pnl":     round(total_value - total_cost, 2),
        "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        "position_count": len(cards),
    }

    return jsonify({"summary": summary, "cards": cards})


@bp.get("/api/sparkline/<ticker>")
def sparkline(ticker: str):
    """
    Returns up to 30 days of closing prices for a ticker.
    Cached in memory for 1 hour so the dashboard doesn't hammer yfinance.
    """
    ticker = ticker.upper()
    now = time.time()

    with _sparkline_lock:
        cached = _sparkline_cache.get(ticker)
        if cached and cached["expires_at"] > now:
            return jsonify(cached["data"])

    # Fetch outside the lock so we don't block other requests
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="30d")
        closes = [round(float(p), 4) for p in hist["Close"].tolist()] if not hist.empty else []
    except Exception:
        closes = []

    data = {"ticker": ticker, "closes": closes}
    with _sparkline_lock:
        _sparkline_cache[ticker] = {"data": data, "expires_at": now + _SPARKLINE_TTL}

    return jsonify(data)
