import logging

from flask import Blueprint, current_app, jsonify, render_template

from ..time_display import format_datetime

bp = Blueprint("discovery", __name__)
logger = logging.getLogger(__name__)

CATEGORY_ORDER = [
    ("growth", "Growth"),
    ("value", "Value"),
    ("dividend", "Dividend Income"),
    ("momentum", "Momentum"),
    ("defensive", "Defensive"),
]


@bp.get("/discovery")
def discovery_page():
    db = current_app.extensions["db"]
    portfolio = current_app.extensions["portfolio"]
    price_cache = current_app.extensions["price_cache"]

    recs = db.get_stock_recommendations()
    analyses = db.get_latest_analyses()
    positions = {p["ticker"]: p for p in db.get_positions()}

    buy_more = []
    for a in analyses:
        if a.get("recommendation") != "BUY":
            continue
        pos = positions.get(a["ticker"], {})
        buy_more.append({
            **a,
            "shares": pos.get("shares") or a.get("shares"),
            "avg_cost": pos.get("avg_cost") or a.get("cost_basis"),
            "current_price": pos.get("current_price") or a.get("current_price"),
            "instrument_name": pos.get("instrument_name"),
            "position_value": pos.get("position_value"),
            "unrealized_pnl": pos.get("unrealized_pnl"),
        })

    generated_display = (
        format_datetime(recs["generated_at"]) if recs.get("generated_at") else None
    )

    return render_template(
        "discovery.html",
        recs=recs,
        categories=CATEGORY_ORDER,
        buy_more=buy_more,
        generated_display=generated_display,
        company_names=price_cache.get_prices().get("names") or {},
        active_page="discovery",
    )


@bp.post("/api/discovery/generate")
def generate():
    analyzer = current_app.extensions["analyzer"]
    portfolio = current_app.extensions["portfolio"]

    if analyzer.status["status"] == "running":
        return jsonify({"error": "Portfolio analysis is running — try again shortly"}), 409

    if analyzer.ideas_status["status"] == "running":
        return jsonify({"error": "Discovery generation already in progress"}), 409

    tickers = [h["ticker"] for h in portfolio.get_holdings()]
    t212 = current_app.extensions["t212"]
    try:
        analyzer.generate_stock_ideas_bg(tickers, t212=t212)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify({"status": "started"}), 202


@bp.get("/api/discovery/status")
def discovery_status():
    analyzer = current_app.extensions["analyzer"]
    return jsonify(analyzer.ideas_status)


@bp.get("/api/discovery/results")
def discovery_results():
    db = current_app.extensions["db"]
    data = db.get_stock_recommendations()
    if data.get("generated_at"):
        data["generated_at_display"] = format_datetime(data["generated_at"])
    return jsonify(data)
