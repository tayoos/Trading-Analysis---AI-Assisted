import time
from threading import Lock

from flask import Blueprint, current_app, jsonify, request

from ..dashboard_build import build_dashboard_view

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

    db = current_app.extensions["db"]
    price_cache = current_app.extensions["price_cache"]
    holdings = portfolio.get_holdings()
    if not holdings:
        return jsonify({"error": "No holdings found. Add stocks.xlsx or sync T212."}), 400

    from ..capital import build_pie_holdings
    holdings = holdings + build_pie_holdings(db, price_cache.get_prices().get("prices", {}))

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
    view        = build_dashboard_view(db, price_cache)
    return jsonify({
        "summary": view["summary"],
        "cards":   view["cards"],
        "pies":    view["pies"],
    })


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

    market_map = {
        p["ticker"]: p.get("market_ticker") or p["ticker"] for p in positions
    }
    result = price_cache.refresh(tickers, market_map)
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

    db = current_app.extensions["db"]
    pos = next((p for p in db.get_positions() if p["ticker"] == ticker), None)
    yf_symbol = (pos or {}).get("market_ticker") or ticker

    try:
        from ..prices import _ticker_candidates
        import yfinance as yf
        closes = []
        for candidate in _ticker_candidates(yf_symbol):
            hist = yf.Ticker(candidate).history(period="30d")
            if not hist.empty:
                closes = [round(float(p), 4) for p in hist["Close"].tolist()]
                break
    except Exception:
        closes = []

    data = {"ticker": ticker, "closes": closes}
    with _sparkline_lock:
        _sparkline_cache[ticker] = {"data": data, "expires_at": now + _SPARKLINE_TTL}

    return jsonify(data)
