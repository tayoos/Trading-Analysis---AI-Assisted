"""
Shared dashboard assembly: cards, pie groups, capital metrics.
"""
from __future__ import annotations

from collections import defaultdict

from .capital import format_pie_icon
from .currency import enrich_position_currencies
from .ticker_resolve import _is_valid_yf_symbol

PIE_TICKER_PREFIX = "PIE:"


def pie_analysis_ticker(pie_id: int) -> str:
    return f"{PIE_TICKER_PREFIX}{pie_id}"


def is_pie_ticker(ticker: str) -> bool:
    return ticker.startswith(PIE_TICKER_PREFIX)


def _quote_symbol(ticker: str, position: dict | None) -> str:
    """yfinance symbol for market quotes (may differ from T212 DB key)."""
    p = position or {}
    return (p.get("market_ticker") or ticker).upper()


def build_card(
    analysis: dict | None,
    position: dict | None,
    live_prices: dict,
    handoff_notes: dict,
    company_names: dict | None = None,
    price_errors: dict | None = None,
) -> dict:
    ticker = (analysis or position or {}).get("ticker", "")
    card = dict(analysis) if analysis else {
        "ticker": ticker,
        "recommendation": None,
        "confidence": None,
        "reasoning": "No analysis run yet — click Run Analysis to analyse this position.",
        "eps_growth_pct": None,
        "pe_ratio": None,
        "analyst_target_mean": None,
        "next_earnings": None,
        "news_sentiment": None,
        "price_target_30d": None,
        "outlook_90d": None,
        "catalysts": None,
        "risks": None,
    }
    p = position or {}
    if not card.get("cost_basis") and p:
        card["cost_basis"] = p.get("avg_cost")
        card["shares"] = p.get("shares")
    shares = float(card.get("shares") or p.get("shares") or 0)
    t212_value = p.get("position_value")
    t212_price = p.get("current_price")

    # T212 wallet fields (synced): all in account currency (e.g. GBP)
    if t212_value is not None:
        card["position_value"] = float(t212_value)
        if shares > 0:
            card["current_price"] = float(t212_price) if t212_price else card["position_value"] / shares
        card["price_source"] = "t212"
    if p.get("unrealized_pnl") is not None:
        card["unrealized_pnl"] = float(p["unrealized_pnl"])

    quote_sym = _quote_symbol(ticker, p)
    if quote_sym != ticker and _is_valid_yf_symbol(quote_sym):
        card["market_ticker"] = quote_sym

    # Live market price: recalculate value only when we trust the quote (no T212 wallet
    # or price is in the same ballpark — avoids UK pence symbols like TM1L → £0.07).
    live_key = quote_sym if quote_sym in live_prices else (
        ticker if ticker in live_prices else None
    )
    if live_key and shares > 0:
        live = float(live_prices[live_key])
        t212_px = card.get("current_price")
        use_live = t212_value is None or (
            t212_px
            and t212_px > 0
            and 0.2 <= (live / t212_px) <= 5.0
        )
        if use_live:
            card["current_price"] = live
            card["position_value"] = round(live * shares, 2)
            card["price_source"] = "market"
            if p.get("unrealized_pnl") is not None and p.get("avg_cost"):
                cost_basis = float(p["avg_cost"]) * shares
                card["unrealized_pnl"] = round(card["position_value"] - cost_basis, 2)
    elif t212_price and card.get("current_price") is None:
        card["current_price"] = float(t212_price)
        card["price_source"] = "t212"
    card["company_name"] = (
        (company_names or {}).get(ticker)
        or p.get("instrument_name")
        or ""
    )
    card["price_error"] = (
        None if card.get("current_price") else (price_errors or {}).get(ticker)
    )
    card["handoff_note"] = handoff_notes.get(ticker)
    if p.get("instrument_currency"):
        card["instrument_currency"] = p["instrument_currency"]
    return card


def build_dashboard_view(
    db,
    price_cache,
) -> dict:
    analyses = db.get_latest_analyses()
    positions = db.get_positions()
    account_currency = db.get_account_currency()
    handoff_notes = db.get_all_handoff_notes()
    pies = db.get_pies()

    price_data = price_cache.get_prices()
    live_prices = price_data["prices"]
    company_names = price_data.get("names") or {}
    price_errors = price_data.get("errors") or {}

    analysis_map = {a["ticker"]: a for a in analyses if not is_pie_ticker(a["ticker"])}
    pie_analysis_map = {a["ticker"]: a for a in analyses if is_pie_ticker(a["ticker"])}
    pos_map = {p["ticker"]: p for p in positions}

    total_cost = sum(p["shares"] * p["avg_cost"] for p in positions)
    positions_market_value = 0.0
    for p in positions:
        ticker = p["ticker"]
        quote_sym = _quote_symbol(ticker, p)
        price = (
            live_prices.get(quote_sym)
            or live_prices.get(ticker)
            or (analysis_map.get(ticker) or {}).get("current_price")
            or p["avg_cost"]
        )
        positions_market_value += price * p["shares"]

    capital = db.get_capital_metrics()
    if capital.get("holdings_cost") is None and total_cost:
        capital["holdings_cost"] = round(total_cost, 2)
    if capital.get("net_deposits") is not None and capital.get("holdings_cost") is not None:
        if capital.get("reinvested") is None:
            capital["reinvested"] = max(
                0.0,
                round(capital["holdings_cost"] - capital["net_deposits"], 2),
            )

    cards_by_ticker: dict[str, dict] = {}
    analysed = set()

    for a in analyses:
        if is_pie_ticker(a["ticker"]):
            continue
        ticker = a["ticker"]
        card = build_card(
            a, pos_map.get(ticker), live_prices, handoff_notes, company_names, price_errors,
        )
        enrich_position_currencies(card, account_currency)
        cards_by_ticker[ticker] = card
        analysed.add(ticker)

    for p in positions:
        ticker = p["ticker"]
        if ticker not in analysed:
            card = build_card(
                None, p, live_prices, handoff_notes, company_names, price_errors,
            )
            enrich_position_currencies(card, account_currency)
            cards_by_ticker[ticker] = card

    # Shares held inside pies (same ticker may appear in multiple pies)
    pie_qty_by_ticker: dict[str, float] = defaultdict(float)
    for pie in pies:
        for inst in pie["instruments"]:
            pie_qty_by_ticker[inst["ticker"]] += float(inst.get("quantity") or 0)

    def _card_for_pie_member(ticker: str, pie_shares: float, pie_value: float | None) -> dict:
        """Card scoped to the slice of a position held inside a pie."""
        base = dict(cards_by_ticker.get(ticker) or build_card(
            None,
            {"ticker": ticker, "shares": pie_shares, "avg_cost": 0},
            live_prices,
            handoff_notes,
            company_names,
            price_errors,
        ))
        base["shares"] = pie_shares
        if pie_value and pie_shares > 0:
            base["cost_basis"] = round(pie_value / pie_shares, 6)
        base["in_pie_only"] = True
        return base

    pie_groups = []
    for pie in pies:
        member_cards = []
        for inst in pie["instruments"]:
            t = inst["ticker"]
            qty = float(inst.get("quantity") or 0)
            if qty <= 0:
                continue
            member_cards.append(
                _card_for_pie_member(t, qty, inst.get("value"))
            )
        pie_key = pie_analysis_ticker(pie["id"])
        pie_analysis = pie_analysis_map.get(pie_key)
        pie_groups.append({
            **pie,
            "icon": format_pie_icon(pie.get("icon")),
            "analysis_ticker": pie_key,
            "analysis": pie_analysis,
            "member_cards": member_cards,
        })

    # Standalone = shares not allocated to any pie (may overlap ticker with pie holdings)
    standalone_cards: list[dict] = []
    for t in sorted(cards_by_ticker):
        pos = pos_map.get(t)
        total_shares = float((pos or {}).get("shares") or cards_by_ticker[t].get("shares") or 0)
        outside_shares = total_shares - pie_qty_by_ticker.get(t, 0)
        if outside_shares <= 0.0001:
            continue
        card = dict(cards_by_ticker[t])
        card["shares"] = round(outside_shares, 6)
        card["in_pie_only"] = False
        if pie_qty_by_ticker.get(t, 0) > 0:
            card["also_in_pie"] = True
        if pos and pos.get("avg_cost"):
            card["cost_basis"] = pos["avg_cost"]
        standalone_cards.append(card)

    account_total = capital.get("account_total_value")
    if account_total:
        total_value = float(account_total)
        value_source = "t212"
    else:
        total_value = positions_market_value
        value_source = "market"

    net = capital.get("net_deposits")
    if net is not None and account_total:
        total_pnl = round(float(account_total) - float(net), 2)
        total_pnl_pct = round(total_pnl / float(net) * 100, 2) if net else 0
    else:
        total_pnl = round(positions_market_value - total_cost, 2)
        total_pnl_pct = (
            round((positions_market_value - total_cost) / total_cost * 100, 2)
            if total_cost
            else 0
        )

    summary = {
        "account_currency":       account_currency,
        "total_value":            round(total_value, 2),
        "value_source":           value_source,
        "positions_market_value": round(positions_market_value, 2),
        "cash_available":         capital.get("cash_available"),
        "total_cost":             round(total_cost, 2),
        "total_pnl":              total_pnl,
        "total_pnl_pct":          total_pnl_pct,
        "position_count":    len(positions),
        "prices_stale":      price_data["stale"],
        "prices_fetched_at": price_data["fetched_at"],
        "dividends":         db.get_dividend_stats(),
        "net_deposits":           capital.get("net_deposits"),
        "net_deposits_estimated": capital.get("net_deposits_estimated"),
        "reinvested":             capital.get("reinvested"),
        "holdings_cost":      capital.get("holdings_cost") or round(total_cost, 2),
        "capital_synced_at":  capital.get("synced_at"),
        "net_deposits_known": capital.get("net_deposits") is not None,
        "capital_error":      capital.get("last_error"),
    }

    capital["account_currency"] = account_currency

    return {
        "summary": summary,
        "capital": capital,
        "account_currency": account_currency,
        "pies": pie_groups,
        "cards": standalone_cards,
        "all_cards": list(cards_by_ticker.values()),
        "handoff_notes": handoff_notes,
        "company_names": price_data["names"],
    }
