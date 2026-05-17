import time
from threading import Lock

from flask import Blueprint, current_app, jsonify, request

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
    db          = current_app.extensions["db"]
    price_cache = current_app.extensions["price_cache"]

    analyses      = db.get_latest_analyses()
    positions     = db.get_positions()
    handoff_notes = db.get_all_handoff_notes()

    analysis_map = {a["ticker"]: a for a in analyses}
    pos_map      = {p["ticker"]: p for p in positions}

    # Live prices from cache (populated by /api/prices/refresh).
    # Priority: live cache → last analysis price → avg_cost (neutral P&L).
    cached       = price_cache.get_prices()
    live_prices  = cached["prices"]
    prices_stale = cached["stale"]

    total_cost  = sum(p["shares"] * p["avg_cost"] for p in positions)
    total_value = 0.0
    for p in positions:
        ticker = p["ticker"]
        price  = (live_prices.get(ticker)
                  or (analysis_map.get(ticker) or {}).get("current_price")
                  or p["avg_cost"])
        total_value += price * p["shares"]

    cards = []
    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        if not a.get("cost_basis") and p:
            a["cost_basis"] = p.get("avg_cost")
            a["shares"]     = p.get("shares")
        # Inject live price into card so per-card P&L uses latest price
        if a["ticker"] in live_prices:
            a["current_price"] = live_prices[a["ticker"]]
        cards.append({**a, "handoff_note": handoff_notes.get(a["ticker"])})

    summary = {
        "total_value":    round(total_value, 2),
        "total_cost":     round(total_cost, 2),
        "total_pnl":      round(total_value - total_cost, 2),
        "total_pnl_pct":  round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        "position_count": len(positions),
        "prices_stale":   prices_stale,
        "prices_fetched_at": cached["fetched_at"],
        "dividends":      db.get_dividend_stats(),
    }

    return jsonify({"summary": summary, "cards": cards})


@bp.get("/api/prices")
def get_prices():
    price_cache = current_app.extensions["price_cache"]
    return jsonify(price_cache.get_prices())


@bp.post("/api/prices/refresh")
def refresh_prices():
    """Fetch live prices for all open positions from yfinance."""
    db          = current_app.extensions["db"]
    price_cache = current_app.extensions["price_cache"]

    positions = db.get_positions()
    tickers   = [p["ticker"] for p in positions]
    if not tickers:
        return jsonify({"error": "No open positions to price"}), 400

    # Run synchronously so the caller gets fresh prices back immediately
    result = price_cache.refresh(tickers)
    return jsonify(result)


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
