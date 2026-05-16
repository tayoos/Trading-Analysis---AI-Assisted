import functools
import hashlib
import hmac
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, request

from .analyzer import StockAnalyzer
from .database import Database
from .portfolio import PortfolioManager
from .routes import analysis_bp, dashboard_bp, history_bp, sync_bp
from .sources.t212 import T212DataSource

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

    _load_env()

    # ── Core services ──────────────────────────────────────────────────────────
    db = Database(os.getenv("DB_PATH", "/data/db/stocks.db"))
    t212 = T212DataSource()
    portfolio = PortfolioManager(db)
    analyzer = StockAnalyzer(db)

    app.extensions["db"] = db
    app.extensions["t212"] = t212
    app.extensions["portfolio"] = portfolio
    app.extensions["analyzer"] = analyzer

    # ── Optional Basic Auth ────────────────────────────────────────────────────
    _setup_auth(app)

    # ── Blueprints ─────────────────────────────────────────────────────────────
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(sync_bp)

    # ── Scheduler ──────────────────────────────────────────────────────────────
    _setup_scheduler(app, analyzer, portfolio, t212)

    return app


def _setup_auth(app: Flask) -> None:
    """
    Opt-in HTTP Basic Auth. Only active when DASHBOARD_USER and DASHBOARD_PASSWORD
    are both set. If either is missing, the app runs without authentication
    (fine for local dev; not recommended for production).
    """
    username = os.getenv("DASHBOARD_USER", "")
    password = os.getenv("DASHBOARD_PASSWORD", "")

    if not (username and password):
        logger.warning(
            "DASHBOARD_USER / DASHBOARD_PASSWORD not set — web UI is unprotected. "
            "Set both env vars to enable Basic Auth."
        )
        return

    # Pre-compute expected digest so comparison is always constant-time
    _expected_user = username.encode()
    _expected_pass_digest = hashlib.sha256(password.encode()).digest()

    @app.before_request
    def require_auth():
        auth = request.authorization
        if not auth:
            return _auth_challenge()

        user_ok = hmac.compare_digest(auth.username.encode(), _expected_user)
        pass_digest = hashlib.sha256(auth.password.encode()).digest()
        pass_ok = hmac.compare_digest(pass_digest, _expected_pass_digest)

        if not (user_ok and pass_ok):
            return _auth_challenge()

    logger.info("Basic Auth enabled for user '%s'", username)


def _auth_challenge() -> Response:
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Stock Analyzer"'},
    )


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _setup_scheduler(app: Flask, analyzer: StockAnalyzer,
                     portfolio: PortfolioManager, t212: T212DataSource) -> None:
    days_raw = os.getenv("SCHEDULE_DAYS", "0,2,5")
    hour = int(os.getenv("SCHEDULE_HOUR", "7"))
    sync_offset = int(os.getenv("T212_SYNC_OFFSET_MINS", "30"))
    sync_enabled = os.getenv("T212_SYNC_ENABLED", "true").lower() == "true"

    try:
        day_of_week = ",".join(str(int(d.strip())) for d in days_raw.split(","))
    except ValueError:
        day_of_week = "0,2,5"

    scheduler = BackgroundScheduler(daemon=True)

    if sync_enabled and t212.is_available():
        sync_minute = 60 - sync_offset if sync_offset < 60 else 0
        sync_hour = hour - 1 if sync_offset >= 60 else hour
        scheduler.add_job(
            func=_sync_job,
            args=[app, t212, portfolio],
            trigger=CronTrigger(day_of_week=day_of_week, hour=sync_hour, minute=sync_minute),
            id="t212_sync",
            name="T212 pre-run sync",
            replace_existing=True,
        )
        logger.info("Scheduled T212 sync at %02d:%02d on days %s", sync_hour, sync_minute, day_of_week)

    scheduler.add_job(
        func=_analysis_job,
        args=[app, analyzer, portfolio],
        trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=0),
        id="analysis",
        name="Stock analysis",
        replace_existing=True,
    )
    logger.info("Scheduled analysis at %02d:00 on days %s", hour, day_of_week)

    scheduler.start()
    app.extensions["scheduler"] = scheduler


def _sync_job(app: Flask, t212: T212DataSource, portfolio: PortfolioManager) -> None:
    with app.app_context():
        logger.info("Running scheduled T212 sync")
        try:
            db = app.extensions["db"]

            last = db.get_last_sync_time()
            orders = t212.get_orders(since=last)
            from .sources.base import Trade
            trades = [
                Trade(
                    order_id=o.order_id, ticker=o.ticker, action=o.action,
                    quantity=o.quantity, price=o.price, total_value=o.total_value,
                    traded_at=o.traded_at,
                )
                for o in orders
            ]
            portfolio.apply_trades(trades)

            existing = db.get_dividends(limit=1)
            last_div = existing[0]["paid_at"] if existing else None
            new_divs = t212.get_dividends(since=last_div)
            if new_divs:
                saved = db.save_dividends(new_divs)
                logger.info("Saved %d new dividend payments", saved)
        except Exception:
            logger.exception("Scheduled T212 sync failed")


def _analysis_job(app: Flask, analyzer: StockAnalyzer, portfolio: PortfolioManager) -> None:
    with app.app_context():
        logger.info("Running scheduled analysis")
        holdings = portfolio.get_holdings()
        if holdings:
            analyzer.run_analysis(holdings)
        else:
            logger.warning("No holdings found for scheduled analysis")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    port = int(os.getenv("PORT", "8765"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
