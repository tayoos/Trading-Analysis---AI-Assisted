import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request

from ..capital import apply_net_deposit, sync_capital_from_t212, sync_pies_from_t212
from ..time_display import display_timezone_name, format_datetime

bp = Blueprint("sync", __name__)
logger = logging.getLogger(__name__)

# Live sync state — readable by the status endpoint so the UI can poll
_sync_state: dict = {"running": False, "message": "", "trades": 0, "divs": 0}
_sync_lock = threading.Lock()

_KEY_WARN_DAYS  = 60   # amber warning
_KEY_ALERT_DAYS = 90   # red alert

@bp.get("/sync")
def sync_page():
    db      = current_app.extensions["db"]
    t212    = current_app.extensions["t212"]
    backup  = current_app.extensions["backup"]

    trades    = db.get_trades(limit=50)
    last_sync = db.get_last_sync_time()
    capital   = db.get_capital_metrics()
    key_ages         = db.get_key_ages()
    backup_list      = backup.list_backups()
    last_backup      = backup.last_backup_time()

    key_warnings = _build_key_warnings(key_ages)
    with _sync_lock:
        sync_running = _sync_state["running"]
    now_utc = datetime.now(timezone.utc).isoformat()
    return render_template(
        "sync.html",
        trades=trades,
        last_sync=last_sync,
        last_sync_display=format_datetime(last_sync),
        last_backup_display=format_datetime(last_backup),
        display_timezone=display_timezone_name(),
        now_display=format_datetime(now_utc),
        capital=capital,
        t212_available=t212.is_available(),
        sync_running=sync_running,
        key_ages=key_ages,
        key_warnings=key_warnings,
        key_warn_days=_KEY_WARN_DAYS,
        key_alert_days=_KEY_ALERT_DAYS,
        backup_list=backup_list[:10],
        last_backup=last_backup,
        backup_configured=backup.is_configured(),
        backup_reachable=backup.destination_reachable(),
        backup_retain_days=backup.retain_days,
        active_page="sync",
    )


# ── T212 sync ──────────────────────────────────────────────────────────────────

@bp.post("/api/capital/net-deposit")
def set_net_deposit():
    """Save net deposit from the Sync page (no API scope or env reload needed)."""
    db = current_app.extensions["db"]
    body = request.get_json(silent=True) or {}
    raw = body.get("amount")
    if raw is None or raw == "":
        return jsonify({"error": "amount is required"}), 400
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    if amount < 0:
        return jsonify({"error": "amount must be zero or positive"}), 400
    capital = apply_net_deposit(db, amount)
    return jsonify({"status": "ok", "capital": capital})


@bp.post("/api/capital/sync")
def sync_capital_only():
    """Fetch account summary + transactions without a full trade sync."""
    t212 = current_app.extensions["t212"]
    db = current_app.extensions["db"]
    if not t212.is_available():
        return jsonify({"error": "TRADING212_API_KEY not configured"}), 400
    capital = sync_capital_from_t212(t212, db, holdings_cost=0.0)
    return jsonify({"status": "ok", "capital": capital})


@bp.get("/api/sync/status")
def sync_status():
    with _sync_lock:
        return jsonify(dict(_sync_state))


@bp.post("/api/sync/t212")
def sync_t212():
    t212      = current_app.extensions["t212"]
    portfolio = current_app.extensions["portfolio"]
    db        = current_app.extensions["db"]

    if not t212.is_available():
        return jsonify({"error": "TRADING212_API_KEY not configured"}), 400

    with _sync_lock:
        if _sync_state["running"]:
            return jsonify({"error": "Sync already in progress"}), 409
        _sync_state.update({"running": True, "message": "Starting…", "trades": 0, "divs": 0})

    def _do_sync():
        try:
            since = db.get_latest_trade_time()
            logger.info("T212 sync starting — most recent trade: %s", since or "none (full history fetch)")

            # Net deposit uses transactions API (6 req/min) — run before orders/pies
            with _sync_lock:
                _sync_state["message"] = "Syncing net deposit & account summary…"
            sync_capital_from_t212(t212, db, holdings_cost=0.0)

            with _sync_lock:
                _sync_state["message"] = "Fetching trades from Trading 212…"
            orders = t212.get_orders(since=since)

            with _sync_lock:
                _sync_state["message"] = f"Saving {len(orders)} trades…"
            portfolio.apply_trades(orders)

            # Reconcile positions against T212's live positions endpoint.
            # _rebuild_positions can zero out positions that were opened before
            # the first sync (missing buy history). T212 is the source of truth
            # for what is currently open, so we upsert live positions on top.
            _reconcile_positions(t212, db)

            with _sync_lock:
                _sync_state["message"] = "Refreshing capital metrics…"
            holdings_cost = sum(
                p["shares"] * p["avg_cost"] for p in db.get_positions()
            )
            sync_capital_from_t212(t212, db, holdings_cost)

            with _sync_lock:
                _sync_state["message"] = "Syncing pies…"
            sync_pies_from_t212(t212, db)

            with _sync_lock:
                _sync_state["trades"] = len(orders)
                _sync_state["message"] = "Fetching dividends…"
            last_div = _last_dividend_sync(db)
            new_divs = t212.get_dividends(since=last_div)
            if new_divs:
                saved = db.save_dividends(new_divs)
                logger.info("Saved %d new dividend payments", saved)

            with _sync_lock:
                _sync_state["divs"] = len(new_divs)
                _sync_state["message"] = f"Done — {len(orders)} trades, {len(new_divs)} dividends"

            db.set_setting("t212_last_synced_at", datetime.now(timezone.utc).isoformat())
            logger.info("T212 sync complete — %d trades, %d dividends", len(orders), len(new_divs))
        except Exception:
            logger.exception("T212 sync failed")
            with _sync_lock:
                _sync_state["message"] = "Sync failed — check logs"
        finally:
            with _sync_lock:
                _sync_state["running"] = False

    threading.Thread(target=_do_sync, daemon=True, name="t212-sync").start()
    return jsonify({"status": "sync_started"}), 202


# ── Backup ─────────────────────────────────────────────────────────────────────

@bp.post("/api/backup")
def trigger_backup():
    backup = current_app.extensions["backup"]
    if not backup.is_configured():
        return jsonify({"error": "BACKUP_PATH not configured"}), 400
    if not backup.destination_reachable():
        return jsonify({"error": "Backup destination not reachable — check the volume is mounted"}), 503

    def _do_backup():
        try:
            result = backup.run()
            logger.info("Manual backup complete: %s", result)
        except Exception:
            logger.exception("Manual backup failed")

    threading.Thread(target=_do_backup, daemon=True, name="manual-backup").start()
    return jsonify({"status": "backup_started"}), 202


@bp.get("/api/backup/list")
def list_backups():
    backup = current_app.extensions["backup"]
    return jsonify({"backups": backup.list_backups()})


# ── Key rotation tracking ──────────────────────────────────────────────────────

@bp.post("/api/keys/mark-rotated")
def mark_key_rotated():
    """
    Records the current timestamp as the last rotation date for a key.
    Body: {"key": "t212"} or {"key": "anthropic"}
    """
    db   = current_app.extensions["db"]
    body = request.get_json(silent=True) or {}
    key  = body.get("key", "")

    mapping = {
        "t212":      "t212_key_rotated_at",
        "anthropic": "anthropic_key_rotated_at",
    }
    if key not in mapping:
        return jsonify({"error": "key must be 't212' or 'anthropic'"}), 400

    db.set_setting(mapping[key], datetime.now(timezone.utc).isoformat())
    return jsonify({"status": "ok", "key": key})


@bp.get("/api/keys/status")
def key_status():
    db       = current_app.extensions["db"]
    ages     = db.get_key_ages()
    warnings = _build_key_warnings(ages)
    return jsonify({"ages": ages, "warnings": warnings})


# ── Portfolio ──────────────────────────────────────────────────────────────────

@bp.get("/api/dividends")
def get_dividends():
    db     = current_app.extensions["db"]
    ticker = request.args.get("ticker")
    return jsonify({
        "dividends": db.get_dividends(ticker=ticker, limit=200),
        "summary":   db.get_dividend_summary(),
    })


@bp.get("/api/portfolio")
def get_portfolio():
    db        = current_app.extensions["db"]
    portfolio = current_app.extensions["portfolio"]
    return jsonify({"holdings": portfolio.get_holdings()})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reconcile_positions(t212, db) -> None:
    """
    Fetch live open positions from T212 and upsert them into the DB.

    This acts as a reconciliation step after _rebuild_positions: if trade
    history is incomplete (positions opened before the first sync), the AVCO
    rebuild incorrectly closes them. T212's /positions endpoint is the
    authoritative source for what is currently open.

    Positions T212 no longer reports are left untouched — _rebuild_positions
    already handles closed position cleanup via owned_history.
    """
    try:
        live = t212.get_positions()
    except Exception:
        logger.warning("Could not fetch live positions for reconciliation — skipping")
        return

    if not live:
        logger.info("T212 reconcile: no open positions reported by T212")
        return

    live_tickers: set[str] = set()
    for pos in live:
        live_tickers.add(pos.ticker)
        db.upsert_position(
            ticker=pos.ticker,
            shares=pos.shares,
            avg_cost=pos.avg_cost,
            source="trading212",
            current_price=pos.current_price,
            instrument_name=pos.instrument_name,
            position_value=pos.position_value,
            unrealized_pnl=pos.unrealized_pnl,
        )
    removed = db.prune_positions_not_in(live_tickers, source="trading212")
    if removed:
        logger.info(
            "T212 reconcile: removed %d stale position(s) not on T212: %s",
            len(removed),
            ", ".join(sorted(removed)),
        )
    logger.info("T212 reconcile: upserted %d live position(s) from T212", len(live))


def _last_dividend_sync(db) -> str | None:
    divs = db.get_dividends(limit=1)
    return divs[0]["paid_at"] if divs else None


def _build_key_warnings(ages: dict) -> list[dict]:
    warnings = []
    labels = {
        "t212_key_rotated_at":      ("Trading 212 API key", "t212"),
        "anthropic_key_rotated_at": ("Anthropic API key",   "anthropic"),
    }
    for db_key, (label, short) in labels.items():
        days = ages.get(db_key)
        if days is None:
            warnings.append({"key": short, "label": label, "level": "info",
                              "message": f"{label} rotation has never been recorded. "
                                         "Click 'Mark as rotated' after you set it up."})
        elif days >= _KEY_ALERT_DAYS:
            warnings.append({"key": short, "label": label, "level": "alert",
                              "message": f"{label} was last rotated {days} days ago. "
                                         "Rotate it now in your provider settings."})
        elif days >= _KEY_WARN_DAYS:
            warnings.append({"key": short, "label": label, "level": "warn",
                              "message": f"{label} was last rotated {days} days ago. "
                                         f"Consider rotating it soon (recommended every {_KEY_ALERT_DAYS} days)."})
    return warnings


# expose for dashboard route
KEY_WARN_DAYS  = _KEY_WARN_DAYS
KEY_ALERT_DAYS = _KEY_ALERT_DAYS
