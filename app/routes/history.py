from flask import Blueprint, current_app, jsonify, render_template

bp = Blueprint("history", __name__)


@bp.get("/history")
def history_page():
    db = current_app.extensions["db"]
    runs = db.list_runs(limit=50)
    return render_template("history.html", runs=runs)


@bp.get("/api/history")
def list_runs():
    db = current_app.extensions["db"]
    runs = db.list_runs(limit=50)
    return jsonify(runs)


@bp.get("/api/run/<int:run_id>")
def get_run(run_id: int):
    db = current_app.extensions["db"]
    run = db.get_run(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    analyses = db.get_analyses_for_run(run_id)
    return jsonify({"run": run, "analyses": analyses})


@bp.get("/ticker/<ticker>")
def ticker_page(ticker: str):
    db = current_app.extensions["db"]
    ticker = ticker.upper()
    history = db.get_ticker_history(ticker)
    handoff = db.get_handoff_note(ticker)
    owned = db.get_owned_history(ticker)
    return render_template(
        "ticker.html",
        ticker=ticker,
        history=history,
        handoff_note=handoff,
        owned_history=owned,
    )


@bp.get("/api/ticker/<ticker>")
def ticker_data(ticker: str):
    db = current_app.extensions["db"]
    ticker = ticker.upper()
    history = db.get_ticker_history(ticker)
    handoff = db.get_handoff_note(ticker)
    trades = db.get_trades(ticker=ticker, limit=50)
    return jsonify({"ticker": ticker, "history": history, "handoff_note": handoff, "trades": trades})
