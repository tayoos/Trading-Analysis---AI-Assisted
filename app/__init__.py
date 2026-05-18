import hashlib
import hmac
import ipaddress
import logging
import os
import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, g, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .analyzer import StockAnalyzer
from .backup import BackupManager
from .database import Database
from .portfolio import PortfolioManager
from .prices import LivePriceCache
from .ratelimit import setup_rate_limiting
from .routes import analysis_bp, dashboard_bp, history_bp, sync_bp
from .sources.t212 import T212DataSource

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    from .logging_setup import configure_logging

    configure_logging()

    app = Flask(__name__, template_folder="../templates")
    app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

    from .currency import card_cost_display, card_price_display, format_money, normalize_currency

    @app.template_filter("money")
    def _money_filter(amount, currency="GBP"):
        return format_money(amount, currency)

    @app.template_filter("money_signed")
    def _money_signed_filter(amount, currency="GBP"):
        return format_money(amount, currency, signed=True)

    @app.template_filter("card_price")
    def _card_price_filter(card, account_currency="GBP"):
        return card_price_display(card, account_currency)

    @app.template_filter("card_cost")
    def _card_cost_filter(card, account_currency="GBP"):
        return card_cost_display(card, account_currency)

    @app.template_global()
    def account_currency_default():
        return normalize_currency(os.getenv("ACCOUNT_CURRENCY", "GBP"))

    # Unwrap X-Forwarded-For / X-Forwarded-Proto from Traefik so that
    # request.remote_addr is always the real client IP, not Traefik's IP.
    # x_for=1 means trust one proxy hop (Traefik); increase if you have
    # multiple proxies in front.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _load_env()
    _setup_access_log(app)

    from .time_display import display_timezone_name, format_datetime
    from datetime import datetime, timezone

    tz = display_timezone_name() or "UTC"
    logger.info(
        "Display timezone TZ=%s (now %s)",
        tz,
        format_datetime(datetime.now(timezone.utc).isoformat()),
    )

    # ── Core services ──────────────────────────────────────────────────────────
    db = Database(os.getenv("DB_PATH", "/data/db/stocks.db"))
    t212 = T212DataSource()
    portfolio = PortfolioManager(db)
    analyzer = StockAnalyzer(db)
    price_cache = LivePriceCache()

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
    app.extensions["price_cache"] = price_cache

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

    # ── Self-heal: rebuild positions if trades exist but positions table is empty ─
    try:
        if db.get_trades(limit=1) and not db.get_positions():
            logger.info("Trades found but no positions — auto-rebuilding positions from trade history")
            portfolio._rebuild_positions()
            logger.info("Auto-rebuild complete: %d positions written", len(db.get_positions()))
    except Exception:
        logger.exception("Auto-rebuild of positions failed — trigger a sync to retry")

    # ── Startup price refresh ──────────────────────────────────────────────────
    try:
        positions = db.get_positions()
        if positions and any(not p.get("market_ticker") for p in positions):
            try:
                from .ticker_resolve import refresh_market_tickers
                n = refresh_market_tickers(db)
                if n:
                    logger.info("Resolved market tickers for %d position(s)", n)
                positions = db.get_positions()
            except Exception:
                logger.exception("Market ticker resolution failed at startup")
        tickers = [p["ticker"] for p in positions]
        market_map = {
            p["ticker"]: p.get("market_ticker") or p["ticker"] for p in positions
        }
        if tickers:
            price_cache.refresh_in_background(tickers, market_map)
            logger.info("Kicked background price refresh for %d positions at startup", len(tickers))
    except Exception:
        logger.exception("Startup price refresh failed")

    try:
        from .reports import ReportGenerator

        obs = ReportGenerator().obsidian_status()
        if obs["ready"]:
            logger.info(
                "Obsidian export ready: vault=%s → %s / %s",
                obs["vault_dir"],
                obs["full_portfolio_dir"],
                obs["individual_stock_dir"],
            )
        elif obs["mount_exists"]:
            logger.warning(
                "Obsidian mount present but export not ready: %s",
                "; ".join(obs["issues"]) or "unknown",
            )
        else:
            logger.info(
                "Obsidian export off (set OBSIDIAN_VAULT_DIR=/obsidian and mount vault at /obsidian)",
            )
    except Exception:
        logger.exception("Obsidian startup check failed")

    return app


def _setup_access_log(app: Flask) -> None:
    """
    Log every request with the real client IP (unwrapped from Traefik's
    X-Forwarded-For) and, when present, the Authelia-authenticated identity
    (Remote-User / Remote-Name / Remote-Email headers injected by Authelia
    after successful forward-auth).
    """
    access_log = logging.getLogger("access")

    @app.before_request
    def _start_timer():
        g._req_start = time.monotonic()

    @app.after_request
    def _log_request(response: Response) -> Response:
        duration_ms = int((time.monotonic() - g.get("_req_start", time.monotonic())) * 1000)

        # Real client IP — ProxyFix has already unwrapped X-Forwarded-For
        client_ip = request.remote_addr or "unknown"

        # Authelia sets these after a successful forward-auth challenge
        authelia_user  = request.headers.get("Remote-User",  "")
        authelia_name  = request.headers.get("Remote-Name",  "")
        authelia_email = request.headers.get("Remote-Email", "")
        authelia_groups = request.headers.get("Remote-Groups", "")

        if authelia_user:
            identity = f"authelia:{authelia_user}"
            if authelia_name:
                identity += f" ({authelia_name})"
            if authelia_email:
                identity += f" <{authelia_email}>"
            if authelia_groups:
                identity += f" groups=[{authelia_groups}]"
        else:
            # Could be Basic Auth, trusted-network bypass, or unauthenticated
            auth = request.authorization
            if auth and auth.username:
                identity = f"basic:{auth.username}"
            else:
                identity = "anon"

        access_log.info(
            '%s "%s %s" %d %dms identity=%s',
            client_ip,
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            identity,
        )
        return response


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


def _schedule_timezone() -> ZoneInfo:
    """Timezone for cron triggers — defaults to TZ (e.g. Europe/London)."""
    name = os.getenv("SCHEDULE_TIMEZONE") or os.getenv("TZ", "Europe/London")
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Invalid schedule timezone %r — using UTC", name)
        return ZoneInfo("UTC")


def _day_names(day_of_week: str) -> str:
    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    parts = []
    for d in day_of_week.split(","):
        try:
            parts.append(labels[int(d.strip())])
        except (ValueError, IndexError):
            pass
    return "/".join(parts) if parts else day_of_week


def _setup_scheduler(app: Flask, analyzer: StockAnalyzer,
                     portfolio: PortfolioManager, t212: T212DataSource,
                     backup: BackupManager) -> None:
    schedule_enabled = os.getenv("SCHEDULE_ENABLED", "true").lower() == "true"
    days_raw = os.getenv("SCHEDULE_DAYS", "0,2,5")
    hour = int(os.getenv("SCHEDULE_HOUR", "3"))
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    sync_enabled = os.getenv("T212_SYNC_ENABLED", "true").lower() == "true"
    tz = _schedule_timezone()

    try:
        day_of_week = ",".join(str(int(d.strip())) for d in days_raw.split(","))
    except ValueError:
        day_of_week = "0,2,5"

    scheduler = BackgroundScheduler(daemon=True)

    if schedule_enabled:
        scheduler.add_job(
            func=_scheduled_sync_and_analysis_job,
            args=[app, t212, portfolio, analyzer, sync_enabled],
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=tz,
            ),
            id="scheduled_run",
            name="T212 sync + analysis",
            replace_existing=True,
        )
        sync_part = "T212 sync then analysis" if (sync_enabled and t212.is_available()) else "analysis only"
        logger.info(
            "Scheduled %s at %02d:%02d %s on %s (days %s)",
            sync_part,
            hour,
            minute,
            tz,
            _day_names(day_of_week),
            day_of_week,
        )
    else:
        logger.info("SCHEDULE_ENABLED=false — automatic sync/analysis disabled (manual runs still work)")

    # Nightly backup at 02:00 UTC
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


def _scheduled_sync_and_analysis_job(
    app: Flask,
    t212: T212DataSource,
    portfolio: PortfolioManager,
    analyzer: StockAnalyzer,
    sync_enabled: bool,
) -> None:
    """Mon/Wed/Sat (configurable): sync portfolio from T212, then run AI analysis."""
    with app.app_context():
        logger.info("Starting scheduled sync + analysis run")
        if sync_enabled and t212.is_available():
            _sync_job(app, t212, portfolio)
        else:
            logger.info("Skipping T212 sync (disabled or API not configured)")
        _analysis_job(app, analyzer, portfolio)


def _sync_job(app: Flask, t212: T212DataSource, portfolio: PortfolioManager) -> None:
    with app.app_context():
        from .routes.sync import _sync_state, _sync_lock

        with _sync_lock:
            if _sync_state["running"]:
                logger.warning("Skipping scheduled T212 sync — manual sync already in progress")
                return

        logger.info("Running scheduled T212 sync")
        try:
            from datetime import datetime, timezone
            db = app.extensions["db"]

            since = db.get_latest_trade_time()
            logger.info("Scheduled sync — most recent trade: %s", since or "none (full history fetch)")
            orders = t212.get_orders(since=since)
            portfolio.apply_trades(orders)

            from .routes.sync import _reconcile_positions
            from .capital import sync_capital_from_t212, sync_pies_from_t212
            sync_capital_from_t212(t212, db, holdings_cost=0.0)
            _reconcile_positions(t212, db)
            holdings_cost = sum(p["shares"] * p["avg_cost"] for p in db.get_positions())
            sync_capital_from_t212(t212, db, holdings_cost)
            sync_pies_from_t212(t212, db)

            existing = db.get_dividends(limit=1)
            last_div = existing[0]["paid_at"] if existing else None
            new_divs = t212.get_dividends(since=last_div)
            if new_divs:
                saved = db.save_dividends(new_divs)
                logger.info("Saved %d new dividend payments", saved)

            db.set_setting("t212_last_synced_at", datetime.now(timezone.utc).isoformat())
            logger.info("Scheduled T212 sync complete")
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
        if analyzer.status["status"] == "running":
            logger.warning("Skipping scheduled analysis — analysis already in progress")
            return
        logger.info("Running scheduled analysis")
        holdings = portfolio.get_holdings()
        if holdings:
            from .capital import build_pie_holdings
            db = app.extensions["db"]
            prices = app.extensions["price_cache"].get_prices().get("prices", {})
            holdings = holdings + build_pie_holdings(db, prices)
            analyzer.run_analysis(holdings)
        else:
            logger.warning("No holdings found for scheduled analysis")


def main() -> None:
    from .logging_setup import configure_logging

    configure_logging()
    try:
        app = create_app()
    except Exception:
        logger.critical("Startup failed", exc_info=True)
        raise SystemExit(1)
    port = int(os.getenv("PORT", "8765"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
