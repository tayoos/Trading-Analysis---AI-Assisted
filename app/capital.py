"""
Capital metrics: net deposits vs reinvested gains/dividends.
"""
import logging
import os
from typing import Optional

from .sources.t212 import T212TransactionsScopeError

logger = logging.getLogger(__name__)

MANUAL_NET_DEPOSIT_KEY = "capital_net_deposit_manual"


def apply_net_deposit(db, amount: float, *, source: str = "manual") -> dict:
    """Persist net deposit (manual entry or override) and recompute reinvested."""
    amount = round(float(amount), 2)
    if source == "manual":
        db.set_setting(MANUAL_NET_DEPOSIT_KEY, str(amount))
    metrics = db.get_capital_metrics()
    cost = metrics.get("holdings_cost")
    if cost is None:
        cost = 0.0
    reinvested = max(0.0, round(float(cost) - amount, 2)) if cost else None
    out = {
        "net_deposits": amount,
        "holdings_cost": round(float(cost), 2) if cost else None,
        "reinvested": reinvested,
    }
    db.save_capital_metrics(out)
    if source == "manual":
        db.set_setting("capital_last_error", "")
    logger.info("Net deposit set (%s): £%.2f", source, amount)
    return db.get_capital_metrics()


def net_deposits_from_transactions(transactions: list[dict]) -> float:
    """
    Sum of deposits minus withdrawals (and outbound transfers).
    Matches Trading 212 'net deposit' — money you put in from outside the account.
    T212 amounts may be signed either way; we always use absolute values per type.
    """
    total = 0.0
    for tx in transactions:
        tx_type = (tx.get("type") or "").upper()
        amount = abs(float(tx.get("amount", 0)))
        if tx_type == "DEPOSIT":
            total += amount
        elif tx_type in ("WITHDRAW", "TRANSFER"):
            total -= amount
        elif tx_type == "FEE":
            total -= amount
    return round(total, 2)


def compute_capital_metrics(
    transactions: list[dict],
    holdings_cost: float,
    account_summary: Optional[dict] = None,
) -> dict:
    """
    investment_amount  = net deposits (your own money in)
    reinvested_amount  = holdings cost basis minus net deposits
                         (dividends, realised gains, pie auto-invest, etc.)
    """
    net = net_deposits_from_transactions(transactions)
    if account_summary and account_summary.get("investments_cost"):
        cost = float(account_summary["investments_cost"])
    else:
        cost = float(holdings_cost)
    reinvested = max(0.0, round(cost - net, 2))
    return {
        "net_deposits":    net,
        "holdings_cost":   round(cost, 2),
        "reinvested":      reinvested,
    }


def sync_capital_from_t212(t212, db, holdings_cost: float) -> dict:
    """Fetch T212 account data and persist capital metrics."""
    if not t212.is_available():
        return db.get_capital_metrics()

    override = os.getenv("NET_DEPOSITS_OVERRIDE", "").strip()
    cached = db.get_capital_metrics()

    try:
        summary = t212.get_account_summary()
    except Exception:
        logger.warning("Could not fetch T212 account summary for capital metrics")
        summary = None

    transactions: list[dict] = []
    tx_error: Optional[str] = None
    try:
        transactions = t212.get_transactions()
    except T212TransactionsScopeError:
        tx_error = "missing_transactions_scope"
        logger.error(
            "Net deposit unavailable — T212 API key needs the "
            "history:transactions permission (see T212 → Settings → API)"
        )
    except Exception:
        tx_error = "transactions_fetch_failed"
        logger.warning("Could not fetch T212 transactions for capital metrics")

    if transactions:
        metrics = compute_capital_metrics(transactions, holdings_cost, summary)
        metrics["transaction_count"] = len(transactions)
        dep = sum(1 for t in transactions if (t.get("type") or "").upper() == "DEPOSIT")
        logger.info(
            "Transactions: %d total, %d deposits → net deposit £%.2f",
            len(transactions),
            dep,
            metrics["net_deposits"],
        )
    else:
        # Do not overwrite a good cached net deposit when the API returns nothing
        logger.warning(
            "No transactions from T212 — keeping cached net deposits (%s)",
            cached.get("net_deposits"),
        )
        cost = float((summary or {}).get("investments_cost") or holdings_cost)
        metrics = {
            "net_deposits":    cached.get("net_deposits"),
            "holdings_cost":   round(cost, 2),
            "reinvested":      None,
            "transaction_count": cached.get("transaction_count") or 0,
        }
        if metrics["net_deposits"] is not None:
            metrics["reinvested"] = max(0.0, round(metrics["holdings_cost"] - metrics["net_deposits"], 2))

    manual = (db.get_setting(MANUAL_NET_DEPOSIT_KEY) or "").strip()
    if manual and not override:
        try:
            metrics["net_deposits"] = round(float(manual), 2)
            metrics["reinvested"] = max(
                0.0,
                round(metrics["holdings_cost"] - metrics["net_deposits"], 2),
            )
            if tx_error:
                logger.info(
                    "Using saved net deposit £%.2f (T212 transactions unavailable)",
                    metrics["net_deposits"],
                )
        except ValueError:
            logger.warning("Invalid %s: %r", MANUAL_NET_DEPOSIT_KEY, manual)

    if override:
        try:
            metrics["net_deposits"] = round(float(override), 2)
            metrics["reinvested"] = max(
                0.0,
                round(metrics["holdings_cost"] - metrics["net_deposits"], 2),
            )
            logger.info("Using NET_DEPOSITS_OVERRIDE=£%.2f", metrics["net_deposits"])
        except ValueError:
            logger.warning("Invalid NET_DEPOSITS_OVERRIDE: %r", override)

    if tx_error:
        db.set_setting("capital_last_error", tx_error)
    elif transactions:
        db.set_setting("capital_last_error", "")

    db.save_capital_metrics(metrics)
    metrics["last_error"] = tx_error
    logger.info(
        "Capital metrics: net_deposits=%s holdings_cost=£%.2f reinvested=%s (%d transactions)",
        metrics.get("net_deposits"),
        metrics["holdings_cost"],
        metrics.get("reinvested"),
        metrics.get("transaction_count", 0),
    )
    return metrics


def pie_display_name(settings: dict, pie_id: int) -> str:
    """Best-effort pie label from T212 settings (often stored in icon)."""
    for key in ("name", "title", "displayName"):
        if settings.get(key):
            return str(settings[key])
    icon = settings.get("icon")
    if icon and len(str(icon).strip()) > 2:
        return str(icon).strip()
    return f"Pie {pie_id}"


def sync_pies_from_t212(t212, db) -> int:
    """Fetch all pies with holdings and store in DB. Returns pie count."""
    if not t212.is_available():
        return 0

    try:
        pie_list = t212.get_pies()
    except Exception:
        logger.warning("Could not fetch T212 pies list")
        return 0

    pies_out: list[dict] = []
    import time as _time

    for summary in pie_list:
        pie_id = int(summary["id"])
        try:
            detail = t212.get_pie(pie_id)
        except Exception:
            logger.warning("Could not fetch pie %s details", pie_id)
            continue

        settings = detail.get("settings") or {}
        div = summary.get("dividendDetails") or {}
        result = summary.get("result") or {}

        instruments = []
        for inst in detail.get("instruments") or []:
            raw = inst.get("ticker", "")
            ticker = t212._normalise_ticker(raw)
            if not ticker:
                continue
            qty = float(inst.get("ownedQuantity", 0))
            if qty <= 0:
                continue
            inst_result = inst.get("result") or {}
            instruments.append({
                "ticker":   ticker,
                "quantity": qty,
                "value":    float(inst_result.get("priceAvgValue", 0)) or None,
            })

        pies_out.append({
            "id":              pie_id,
            "name":            pie_display_name(settings, pie_id),
            "icon":            settings.get("icon"),
            "cash":            float(summary.get("cash", 0)),
            "reinvested":      float(div.get("reinvested", 0)),
            "invested_value":  float(result.get("priceAvgInvestedValue", 0)),
            "current_value":   float(result.get("priceAvgValue", 0)),
            "instruments":     instruments,
        })
        _time.sleep(5.1)  # pies detail rate limit: 1 req / 5s

    db.replace_pies(pies_out)
    logger.info("Synced %d pie(s) with holdings", len(pies_out))
    return len(pies_out)


def build_pie_holdings(db, live_prices: dict | None = None) -> list[dict]:
    """Synthetic holdings for combined pie-level AI analysis."""
    live_prices = live_prices or {}
    pos_map = {p["ticker"]: p for p in db.get_positions()}
    holdings = []
    for pie in db.get_pies():
        if not pie.get("instruments"):
            continue
        members = []
        total_cost = 0.0
        total_value = 0.0
        for inst in pie["instruments"]:
            t = inst["ticker"]
            p = pos_map.get(t, {})
            shares = float(p.get("shares") or inst.get("quantity") or 0)
            cost = float(p.get("avg_cost") or 0)
            price = live_prices.get(t) or cost
            total_cost += shares * cost
            total_value += shares * price
            members.append({"ticker": t, "shares": shares, "avg_cost": cost})

        holdings.append({
            "ticker":       f"PIE:{pie['id']}",
            "shares":       1,
            "avg_cost":     pie.get("invested_value") or total_cost,
            "current_price": pie.get("current_value") or total_value,
            "is_pie":       True,
            "pie_name":     pie["name"],
            "pie_id":       pie["id"],
            "pie_members":  members,
        })
    return holdings
