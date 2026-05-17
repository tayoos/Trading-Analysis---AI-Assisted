from flask import Blueprint, current_app, render_template

from .sync import _build_key_warnings

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    db       = current_app.extensions["db"]
    analyses = db.get_latest_analyses()
    positions = db.get_positions()
    pos_map  = {p["ticker"]: p for p in positions}
    handoff_notes = db.get_all_handoff_notes()

    cards = []
    analysed = set()
    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        if not a.get("cost_basis") and p:
            a["cost_basis"] = p.get("avg_cost")
            a["shares"]     = p.get("shares")
        cards.append(a)
        analysed.add(a["ticker"])

    # Show positions that haven't been analysed yet as bare cards
    for p in positions:
        if p["ticker"] not in analysed:
            cards.append({
                "ticker":             p["ticker"],
                "shares":             p.get("shares"),
                "cost_basis":         p.get("avg_cost"),
                "recommendation":     None,
                "current_price":      None,
                "confidence":         None,
                "reasoning":          "No analysis run yet — click Run Analysis to analyse this position.",
                "eps_growth_pct":     None,
                "pe_ratio":           None,
                "analyst_target_mean": None,
                "next_earnings":      None,
                "news_sentiment":     None,
                "price_target_30d":   None,
                "outlook_90d":        None,
                "catalysts":          None,
                "risks":              None,
            })

    key_warnings  = _build_key_warnings(db.get_key_ages())
    dividend_stats = db.get_dividend_stats()

    return render_template(
        "dashboard.html",
        cards=cards,
        handoff_notes=handoff_notes,
        analyzer_status=current_app.extensions["analyzer"].status,
        key_warnings=key_warnings,
        dividend_stats=dividend_stats,
        active_page="dashboard",
    )
