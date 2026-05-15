from flask import Blueprint, current_app, render_template

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    db = current_app.extensions["db"]
    analyses = db.get_latest_analyses()
    positions = db.get_positions()
    pos_map = {p["ticker"]: p for p in positions}

    # Merge position cost data with latest analysis
    cards = []
    for a in analyses:
        p = pos_map.get(a["ticker"], {})
        if not a.get("cost_basis") and p:
            a["cost_basis"] = p.get("avg_cost")
            a["shares"] = p.get("shares")
        cards.append(a)

    handoff_notes = {
        t: db.get_handoff_note(t)
        for t in [a["ticker"] for a in analyses]
    }

    return render_template(
        "dashboard.html",
        cards=cards,
        handoff_notes=handoff_notes,
        analyzer_status=current_app.extensions["analyzer"].status,
    )
