"""
Shared dashboard assembly: cards, pie groups, capital metrics.
"""
from __future__ import annotations

PIE_TICKER_PREFIX = "PIE:"


def pie_analysis_ticker(pie_id: int) -> str:
    return f"{PIE_TICKER_PREFIX}{pie_id}"


def is_pie_ticker(ticker: str) -> bool:
    return ticker.startswith(PIE_TICKER_PREFIX)


def build_card(
    analysis: dict | None,
    position: dict | None,
    live_prices: dict,
    handoff_notes: dict,
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
    if ticker in live_prices:
        card["current_price"] = live_prices[ticker]
    card["handoff_note"] = handoff_notes.get(ticker)
    return card


def build_dashboard_view(
    db,
    price_cache,
) -> dict:
    analyses = db.get_latest_analyses()
    positions = db.get_positions()
    handoff_notes = db.get_all_handoff_notes()
    pies = db.get_pies()
    pie_tickers = db.get_pie_tickers()

    price_data = price_cache.get_prices()
    live_prices = price_data["prices"]

    analysis_map = {a["ticker"]: a for a in analyses if not is_pie_ticker(a["ticker"])}
    pie_analysis_map = {a["ticker"]: a for a in analyses if is_pie_ticker(a["ticker"])}
    pos_map = {p["ticker"]: p for p in positions}

    total_cost = sum(p["shares"] * p["avg_cost"] for p in positions)
    total_value = 0.0
    for p in positions:
        ticker = p["ticker"]
        price = (
            live_prices.get(ticker)
            or (analysis_map.get(ticker) or {}).get("current_price")
            or p["avg_cost"]
        )
        total_value += price * p["shares"]

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
        cards_by_ticker[ticker] = build_card(a, pos_map.get(ticker), live_prices, handoff_notes)
        analysed.add(ticker)

    for p in positions:
        ticker = p["ticker"]
        if ticker not in analysed:
            cards_by_ticker[ticker] = build_card(None, p, live_prices, handoff_notes)

    pie_groups = []
    for pie in pies:
        member_cards = []
        for inst in pie["instruments"]:
            t = inst["ticker"]
            if t in cards_by_ticker:
                member_cards.append(cards_by_ticker[t])
        pie_key = pie_analysis_ticker(pie["id"])
        pie_analysis = pie_analysis_map.get(pie_key)
        pie_groups.append({
            **pie,
            "analysis_ticker": pie_key,
            "analysis": pie_analysis,
            "member_cards": member_cards,
        })

    standalone_cards = [
        cards_by_ticker[t]
        for t in sorted(cards_by_ticker)
        if t not in pie_tickers
    ]

    summary = {
        "total_value":       round(total_value, 2),
        "total_cost":        round(total_cost, 2),
        "total_pnl":         round(total_value - total_cost, 2),
        "total_pnl_pct":     round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        "position_count":    len(positions),
        "prices_stale":      price_data["stale"],
        "prices_fetched_at": price_data["fetched_at"],
        "dividends":         db.get_dividend_stats(),
        "net_deposits":       capital.get("net_deposits"),
        "reinvested":         capital.get("reinvested"),
        "holdings_cost":      capital.get("holdings_cost") or round(total_cost, 2),
        "capital_synced_at":  capital.get("synced_at"),
        "net_deposits_known": capital.get("net_deposits") is not None,
        "capital_error":      capital.get("last_error"),
    }

    return {
        "summary": summary,
        "capital": capital,
        "pies": pie_groups,
        "cards": standalone_cards,
        "all_cards": list(cards_by_ticker.values()),
        "handoff_notes": handoff_notes,
        "company_names": price_data["names"],
    }
