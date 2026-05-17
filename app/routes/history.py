from flask import Blueprint, current_app, jsonify, render_template

bp = Blueprint("history", __name__)


@bp.get("/history")
def history_page():
    db = current_app.extensions["db"]
    runs = db.list_runs(limit=50)
    return render_template("history.html", runs=runs, active_page="history")


@bp.get("/api/trades")
def get_trades():
    db = current_app.extensions["db"]
    trades = db.get_trades(limit=500)
    owned = db.get_owned_history()
    return jsonify({"trades": trades, "owned_history": owned})


@bp.get("/api/dividends-history")
def get_dividends_history():
    db = current_app.extensions["db"]
    dividends = db.get_dividends(limit=500)
    summary = db.get_dividend_summary()
    return jsonify({"dividends": dividends, "summary": summary})


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
    dividends = db.get_dividends(ticker=ticker)
    total_dividends = sum(d["amount"] for d in dividends)
    return render_template(
        "ticker.html",
        ticker=ticker,
        history=history,
        handoff_note=handoff,
        owned_history=owned,
        dividends=dividends,
        total_dividends=total_dividends,
    )


@bp.get("/api/ticker/<ticker>")
def ticker_data(ticker: str):
    db = current_app.extensions["db"]
    ticker = ticker.upper()
    history = db.get_ticker_history(ticker)
    handoff = db.get_handoff_note(ticker)
    trades = db.get_trades(ticker=ticker, limit=50)
    return jsonify({"ticker": ticker, "history": history, "handoff_note": handoff, "trades": trades})
