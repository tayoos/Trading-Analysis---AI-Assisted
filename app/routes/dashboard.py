from flask import Blueprint, current_app, render_template

from ..dashboard_build import build_dashboard_view
from .sync import _build_key_warnings

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    db          = current_app.extensions["db"]
    portfolio   = current_app.extensions["portfolio"]
    price_cache = current_app.extensions["price_cache"]

    purged = portfolio.purge_dust_positions()
    if purged:
        current_app.logger.info("Purged dust position(s): %s", ", ".join(sorted(purged)))

    positions = db.get_positions()
    tickers = [p["ticker"] for p in positions]
    market_map = {
        p["ticker"]: p.get("market_ticker") or p["ticker"] for p in positions
    }
    if tickers:
        snap = price_cache.get_prices()
        names = snap.get("names") or {}
        if price_cache.is_stale() or sum(1 for t in tickers if t not in names) > len(tickers) // 2:
            price_cache.refresh(tickers, market_map)

    view = build_dashboard_view(db, price_cache)
    key_warnings   = _build_key_warnings(db.get_key_ages())
    dividend_stats = db.get_dividend_stats()

    return render_template(
        "dashboard.html",
        cards=view["cards"],
        pies=view["pies"],
        summary=view["summary"],
        capital=view["capital"],
        account_currency=view["account_currency"],
        handoff_notes=view["handoff_notes"],
        analyzer_status=current_app.extensions["analyzer"].status,
        key_warnings=key_warnings,
        dividend_stats=dividend_stats,
        company_names=view["company_names"],
        active_page="dashboard",
    )
