from flask import Blueprint, current_app, render_template

from .sync import _build_key_warnings

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    db       = current_app.extensions["db"]
    analyses = db.get_latest_analyses()
    pos_map  = {p["ticker"]: p for p in db.get_positions()}
    handoff_notes = db.get_all_handoff_notes()

    cards = []
    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        if not a.get("cost_basis") and p:
            a["cost_basis"] = p.get("avg_cost")
            a["shares"]     = p.get("shares")
        cards.append(a)

    key_warnings  = _build_key_warnings(db.get_key_ages())
    dividend_stats = db.get_dividend_stats()

    return render_template(
        "dashboard.html",
        cards=cards,
        handoff_notes=handoff_notes,
        analyzer_status=current_app.extensions["analyzer"].status,
        key_warnings=key_warnings,
        dividend_stats=dividend_stats,
    )
