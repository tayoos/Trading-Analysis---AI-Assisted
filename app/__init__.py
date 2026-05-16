import hashlib
import hmac
import ipaddress
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, request

from .analyzer import StockAnalyzer
from .backup import BackupManager
from .database import Database
from .portfolio import PortfolioManager
from .ratelimit import setup_rate_limiting
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

    backup = BackupManager(
        db_path=os.getenv("DB_PATH", "/data/db/stocks.db"),
        reports_dir=os.getenv("REPORTS_DIR", "/data/reports"),
        backup_path="/backups",
        retain_days=int(os.getenv("BACKUP_RETAIN_DAYS", "60")),
    )

    app.extensions["db"] = db
    app.extensions["t212"] = t212
    app.extensions["portfolio"] = portfolio
    app.extensions["analyzer"] = analyzer
    app.extensions["backup"] = backup

    # ── Auth + rate limiting ───────────────────────────────────────────────────
    trusted_networks = _parse_trusted_networks()
    _setup_auth(app, trusted_networks)
    setup_rate_limiting(app, trusted_networks)

    # ── Blueprints ─────────────────────────────────────────────────────────────
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(sync_bp)

    # ── Scheduler ──────────────────────────────────────────────────────────────
    _setup_scheduler(app, analyzer, portfolio, t212, backup)

    return app


def _parse_trusted_networks() -> list:
    trusted_raw = os.getenv("TRUSTED_NETWORKS", "")
    networks = []
    for cidr in trusted_raw.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Invalid TRUSTED_NETWORKS entry (ignored): %r", cidr)
    if networks:
        logger.info("Trusted networks: %s", ", ".join(str(n) for n in networks))
    return networks


def _setup_auth(app: Flask, trusted_networks: list) -> None:
    """
    Two-layer auth:
      Layer 1 — requests from TRUSTED_NETWORKS bypass Basic Auth (proxy already authed them).
      Layer 2 — Basic Auth via DASHBOARD_USER + DASHBOARD_PASSWORD for direct LAN access.
    Both disabled → app runs open with a warning (local dev only).
    """
    username = os.getenv("DASHBOARD_USER", "")
    password = os.getenv("DASHBOARD_PASSWORD", "")

    has_basic_auth = bool(username and password)

    if not has_basic_auth and not trusted_networks:
        logger.warning(
            "No auth configured — web UI is unprotected. "
            "Set DASHBOARD_USER+DASHBOARD_PASSWORD or TRUSTED_NETWORKS."
        )
        return

    if has_basic_auth:
        _expected_user        = username.encode()
        _expected_pass_digest = hashlib.sha256(password.encode()).digest()
    else:
        _expected_user        = b""
        _expected_pass_digest = b""

    def _from_trusted_ip() -> bool:
        if not trusted_networks:
            return False
        raw = request.remote_addr or ""
        try:
            addr = ipaddress.ip_address(raw)
            return any(addr in net for net in trusted_networks)
        except ValueError:
            return False

    def _basic_auth_ok() -> bool:
        if not has_basic_auth:
            return False
        auth = request.authorization
        if not auth:
            return False
        user_ok = hmac.compare_digest(auth.username.encode(), _expected_user)
        pass_ok = hmac.compare_digest(
            hashlib.sha256(auth.password.encode()).digest(),
            _expected_pass_digest,
        )
        return user_ok and pass_ok

    @app.before_request
    def require_auth():
        if _from_trusted_ip():
            return None          # proxy already authenticated the user
        if _basic_auth_ok():
            return None
        if has_basic_auth:
            return _auth_challenge()
        # Trusted networks configured but request is not from one of them
        return Response("Forbidden — access via proxy only.", 403)

    if has_basic_auth:
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
                     portfolio: PortfolioManager, t212: T212DataSource,
                     backup: BackupManager) -> None:
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
        sync_hour   = hour - 1 if sync_offset >= 60 else hour
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

    # Nightly backup at 02:00 UTC (after the app has been quiet overnight)
    if backup.is_configured():
        scheduler.add_job(
            func=_backup_job,
            args=[app, backup],
            trigger=CronTrigger(hour=2, minute=0),
            id="backup",
            name="Nightly backup",
            replace_existing=True,
        )
        logger.info("Nightly backup scheduled at 02:00 UTC → %s", backup.backup_path)
    else:
        logger.info("BACKUP_PATH not set — automatic backups disabled")

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


def _backup_job(app: Flask, backup: BackupManager) -> None:
    with app.app_context():
        logger.info("Running nightly backup")
        try:
            result = backup.run()
            logger.info("Backup done: %s", result)
        except Exception:
            logger.exception("Nightly backup failed")


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
