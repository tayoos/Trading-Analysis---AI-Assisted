import threading

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("analysis", __name__)


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
        cost = a.get("cost_basis") or p.get("avg_cost") or 0
        shares = a.get("shares") or p.get("shares") or 0
        price = a.get("current_price") or 0
        value = price * shares
        invested = cost * shares
        total_value += value
        total_cost += invested

        handoff = db.get_handoff_note(a["ticker"])
        cards.append({**a, "handoff_note": handoff})

    summary = {
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_value - total_cost, 2),
        "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        "position_count": len(cards),
    }

    return jsonify({"summary": summary, "cards": cards})
