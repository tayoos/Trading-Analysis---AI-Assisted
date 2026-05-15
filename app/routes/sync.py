import threading

from flask import Blueprint, current_app, jsonify, render_template, request

bp = Blueprint("sync", __name__)


@bp.get("/sync")
def sync_page():
    db = current_app.extensions["db"]
    t212 = current_app.extensions["t212"]
    trades = db.get_trades(limit=50)
    owned = db.get_owned_history()
    dividends = db.get_dividends(limit=50)
    dividend_summary = db.get_dividend_summary()
    last_sync = db.get_last_sync_time()
    return render_template(
        "sync.html",
        trades=trades,
        owned_history=owned,
        dividends=dividends,
        dividend_summary=dividend_summary,
        last_sync=last_sync,
        t212_available=t212.is_available(),
    )


@bp.post("/api/sync/t212")
def sync_t212():
    t212 = current_app.extensions["t212"]
    portfolio = current_app.extensions["portfolio"]
    db = current_app.extensions["db"]

    if not t212.is_available():
        return jsonify({"error": "TRADING212_API_KEY not configured"}), 400

    def _do_sync():
        try:
            last_sync = db.get_last_sync_time()
            orders = t212.get_orders(since=last_sync)
            portfolio.apply_trades(orders)

            # Sync dividends in the same pass
            last_div = _last_dividend_sync(db)
            new_divs = t212.get_dividends(since=last_div)
            if new_divs:
                saved = db.save_dividends(new_divs)
                current_app.logger.info("Saved %d new dividend payments", saved)
        except Exception:
            current_app.logger.exception("T212 sync failed")

    threading.Thread(target=_do_sync, daemon=True, name="t212-sync").start()
    return jsonify({"status": "sync_started"}), 202


@bp.get("/api/dividends")
def get_dividends():
    db = current_app.extensions["db"]
    ticker = request.args.get("ticker")
    dividends = db.get_dividends(ticker=ticker, limit=200)
    summary = db.get_dividend_summary()
    return jsonify({"dividends": dividends, "summary": summary})


@bp.get("/api/portfolio")
def get_portfolio():
    db = current_app.extensions["db"]
    portfolio = current_app.extensions["portfolio"]
    holdings = portfolio.get_holdings()
    return jsonify({"holdings": holdings})


def _last_dividend_sync(db) -> str | None:
    divs = db.get_dividends(limit=1)
    return divs[0]["paid_at"] if divs else None
