from flask import Blueprint, current_app, jsonify, render_template

bp = Blueprint("discovery", __name__)


@bp.get("/discovery")
def discovery_page():
    db          = current_app.extensions["db"]
    analyzer    = current_app.extensions["analyzer"]
    portfolio   = current_app.extensions["portfolio"]

    recs     = db.get_stock_recommendations()
    analyses = db.get_latest_analyses()
    buy_more = [a for a in analyses if a.get("recommendation") == "BUY"]
    pos_map  = {p["ticker"]: p for p in db.get_positions()}

    for item in buy_more:
        p = pos_map.get(item["ticker"], {})
        if not item.get("cost_basis") and p:
            item["cost_basis"] = p.get("avg_cost")
            item["shares"]     = p.get("shares")

    return render_template(
        "discovery.html",
        recs=recs,
        buy_more=buy_more,
        active_page="discovery",
        ideas_status=analyzer.ideas_status,
    )


@bp.post("/api/discovery/generate")
def generate():
    analyzer  = current_app.extensions["analyzer"]
    portfolio = current_app.extensions["portfolio"]
    tickers   = [h["ticker"] for h in portfolio.get_holdings()]
    analyzer.generate_stock_ideas_bg(tickers)
    return jsonify({"status": "started"}), 202


@bp.get("/api/discovery/status")
def discovery_status():
    analyzer = current_app.extensions["analyzer"]
    return jsonify(analyzer.ideas_status)


@bp.get("/api/discovery/results")
def discovery_results():
    db = current_app.extensions["db"]
    return jsonify(db.get_stock_recommendations())
