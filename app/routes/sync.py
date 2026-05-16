import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request

bp = Blueprint("sync", __name__)

_KEY_WARN_DAYS  = 60   # amber warning
_KEY_ALERT_DAYS = 90   # red alert


@bp.get("/sync")
def sync_page():
    db      = current_app.extensions["db"]
    t212    = current_app.extensions["t212"]
    backup  = current_app.extensions["backup"]

    trades           = db.get_trades(limit=50)
    owned            = db.get_owned_history()
    dividends        = db.get_dividends(limit=50)
    dividend_summary = db.get_dividend_summary()
    last_sync        = db.get_last_sync_time()
    key_ages         = db.get_key_ages()
    backup_list      = backup.list_backups()
    last_backup      = backup.last_backup_time()

    return render_template(
        "sync.html",
        trades=trades,
        owned_history=owned,
        dividends=dividends,
        dividend_summary=dividend_summary,
        last_sync=last_sync,
        t212_available=t212.is_available(),
        key_ages=key_ages,
        key_warn_days=_KEY_WARN_DAYS,
        key_alert_days=_KEY_ALERT_DAYS,
        backup_list=backup_list[:10],
        last_backup=last_backup,
        backup_configured=backup.is_configured(),
        backup_reachable=backup.destination_reachable(),
    )


# ── T212 sync ──────────────────────────────────────────────────────────────────

@bp.post("/api/sync/t212")
def sync_t212():
    t212      = current_app.extensions["t212"]
    portfolio = current_app.extensions["portfolio"]
    db        = current_app.extensions["db"]

    if not t212.is_available():
        return jsonify({"error": "TRADING212_API_KEY not configured"}), 400

    def _do_sync():
        try:
            last_sync = db.get_last_sync_time()
            orders    = t212.get_orders(since=last_sync)
            portfolio.apply_trades(orders)

            last_div = _last_dividend_sync(db)
            new_divs = t212.get_dividends(since=last_div)
            if new_divs:
                saved = db.save_dividends(new_divs)
                current_app.logger.info("Saved %d new dividend payments", saved)
        except Exception:
            current_app.logger.exception("T212 sync failed")

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
            current_app.logger.info("Manual backup complete: %s", result)
        except Exception:
            current_app.logger.exception("Manual backup failed")

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
