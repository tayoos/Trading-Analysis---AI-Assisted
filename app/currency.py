"""
Trading 212 currency helpers.

T212 wallet fields (avg cost, current value, P&L) are in the account currency (e.g. GBP).
Instrument listings may use another code (USD, EUR, GBX pence). yfinance quotes use the
listing currency — do not mix with T212 wallet figures without labeling.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_ACCOUNT_CURRENCY = "GBP"

_SYMBOLS: dict[str, str] = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "CHF": "CHF ",
    "CAD": "C$",
    "AUD": "A$",
    "SEK": "kr",
    "NOK": "kr",
    "DKK": "kr",
    "PLN": "zł",
    "HKD": "HK$",
    "JPY": "¥",
}


def normalize_currency(code: Optional[str]) -> str:
    """Uppercase ISO code; default GBP."""
    c = (code or "").strip().upper()
    return c if c else DEFAULT_ACCOUNT_CURRENCY


def currency_symbol(code: Optional[str]) -> str:
    c = normalize_currency(code)
    return _SYMBOLS.get(c, f"{c} ")


def format_money(
    amount: Optional[float],
    currency: Optional[str] = None,
    *,
    decimals: Optional[int] = None,
    signed: bool = False,
) -> str:
    """Format amount with currency symbol (account currency for T212 figures)."""
    if amount is None:
        return "—"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "—"
    c = normalize_currency(currency)
    sym = currency_symbol(c)
    if decimals is None:
        abs_v = abs(value)
        if abs_v >= 1:
            decimals = 2
        elif abs_v >= 0.01:
            decimals = 4
        else:
            decimals = 6
    text = f"{abs(value):,.{decimals}f}"
    if signed:
        prefix = "+" if value >= 0 else "-"
        return f"{prefix}{sym}{text}"
    if value < 0:
        return f"-{sym}{text}"
    return f"{sym}{text}"


def format_price_per_share(
    amount: Optional[float],
    currency: Optional[str] = None,
) -> str:
    """Per-share price with currency label."""
    if amount is None:
        return "—"
    return f"{format_money(amount, currency)}/share ({normalize_currency(currency)})"


def instrument_currency_label(code: Optional[str]) -> str:
    c = (code or "").strip().upper()
    if not c:
        return "—"
    if c == "GBX":
        return "GBX (pence on LSE)"
    return c


def currency_position_note(
    account_currency: Optional[str],
    instrument_currency: Optional[str],
) -> Optional[str]:
    """Short UI hint when listing currency differs from T212 wallet currency."""
    acct = normalize_currency(account_currency)
    inst = (instrument_currency or "").strip().upper()
    if not inst or inst == acct:
        return None
    if inst == "GBX":
        return f"Listed in GBX (pence); T212 cost & value in {acct}"
    return f"Listed in {inst}; T212 avg cost & value in {acct}"


def listing_currency_for(
    instrument_currency: Optional[str],
    quote_currency: Optional[str],
    account_currency: Optional[str],
) -> str:
    """Currency used for exchange listing / yfinance quotes."""
    acct = normalize_currency(account_currency)
    inst = (instrument_currency or "").strip().upper()
    if inst == "GBX":
        return acct
    if inst and inst != acct:
        return inst
    return normalize_currency(quote_currency or inst or acct)


def currencies_differ(a: Optional[str], b: Optional[str]) -> bool:
    return normalize_currency(a) != normalize_currency(b)


def format_price_dual(
    listing_amount: Optional[float],
    listing_ccy: Optional[str],
    account_amount: Optional[float],
    account_ccy: Optional[str],
) -> str:
    """e.g. $1.03 (£0.97) when listing is USD and T212 wallet is GBP."""
    acct = normalize_currency(account_ccy)
    listing = normalize_currency(listing_ccy or acct)

    if not currencies_differ(listing, acct):
        return format_money(
            account_amount if account_amount is not None else listing_amount,
            acct,
        )

    parts: list[str] = []
    if listing_amount is not None:
        parts.append(format_money(listing_amount, listing))
    if account_amount is not None:
        parts.append(f"({format_money(account_amount, acct)})")
    return " ".join(parts) if parts else "—"


def card_price_display(card: dict, account_currency: Optional[str] = None) -> str:
    acct = normalize_currency(account_currency or card.get("account_currency"))
    listing = card.get("listing_currency") or card.get("quote_currency")
    if listing and currencies_differ(listing, acct):
        return format_price_dual(
            card.get("market_price") or card.get("current_price_listing"),
            listing,
            card.get("current_price_account") or card.get("current_price"),
            acct,
        )
    return format_money(
        card.get("current_price_account") or card.get("current_price"),
        acct,
    )


def card_cost_display(card: dict, account_currency: Optional[str] = None) -> str:
    acct = normalize_currency(account_currency or card.get("account_currency"))
    listing = card.get("listing_currency") or card.get("quote_currency")
    if listing and currencies_differ(listing, acct):
        return format_price_dual(
            card.get("avg_cost_listing"),
            listing,
            card.get("avg_cost_account") or card.get("cost_basis"),
            acct,
        )
    return format_money(card.get("avg_cost_account") or card.get("cost_basis"), acct)


def enrich_position_currencies(
    holding: dict,
    account_currency: Optional[str],
    *,
    quote_currency: Optional[str] = None,
) -> dict:
    """Attach currency fields for prompts, UI, and reports."""
    acct = normalize_currency(account_currency)
    inst = (holding.get("instrument_currency") or "").strip().upper() or None
    holding["account_currency"] = acct
    if inst:
        holding["instrument_currency"] = inst
    if quote_currency:
        holding["quote_currency"] = quote_currency.strip().upper()
    holding["currency_note"] = currency_position_note(acct, inst)
    return holding
