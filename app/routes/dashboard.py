from flask import Blueprint, current_app, render_template

from .sync import _build_key_warnings

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    db          = current_app.extensions["db"]
    price_cache = current_app.extensions["price_cache"]

    analyses  = db.get_latest_analyses()
    positions = db.get_positions()
    pos_map   = {p["ticker"]: p for p in positions}
    handoff_notes = db.get_all_handoff_notes()

    live_prices = price_cache.get_prices()["prices"]

    cards = []
    analysed = set()
    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        if not a.get("cost_basis") and p:
            a["cost_basis"] = p.get("avg_cost")
            a["shares"]     = p.get("shares")
        # Use live price if available, fall back to last analysis price
        if a["ticker"] in live_prices:
            a["current_price"] = live_prices[a["ticker"]]
        cards.append(a)
        analysed.add(a["ticker"])

    # Show positions that haven't been analysed yet as bare cards
    for p in positions:
        if p["ticker"] not in analysed:
            cards.append({
                "ticker":              p["ticker"],
                "shares":              p.get("shares"),
                "cost_basis":          p.get("avg_cost"),
                "current_price":       live_prices.get(p["ticker"]),
                "recommendation":      None,
                "confidence":          None,
                "reasoning":           "No analysis run yet — click Run Analysis to analyse this position.",
                "eps_growth_pct":      None,
                "pe_ratio":            None,
                "analyst_target_mean": None,
                "next_earnings":       None,
                "news_sentiment":      None,
                "price_target_30d":    None,
                "outlook_90d":         None,
                "catalysts":           None,
                "risks":               None,
            })

    key_warnings   = _build_key_warnings(db.get_key_ages())
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

