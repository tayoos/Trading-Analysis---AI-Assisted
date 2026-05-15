"""
Report generation: Excel (.xlsx) and plain-text formats.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

_COLOURS = {
    "BUY": "2196F3",
    "HOLD": "9E9E9E",
    "SELL": "F44336",
    "WATCH": "FF9800",
    "POSITIVE": "4CAF50",
    "NEUTRAL": "9E9E9E",
    "NEGATIVE": "F44336",
    "HIGH": "4CAF50",
    "MEDIUM": "FF9800",
    "LOW": "F44336",
}


class ReportGenerator:
    def __init__(self, reports_dir: Optional[str] = None):
        self.reports_dir = reports_dir or os.getenv("REPORTS_DIR", "/data/reports")
        os.makedirs(self.reports_dir, exist_ok=True)

    # ── Excel ──────────────────────────────────────────────────────────────────

    def generate_excel(self, run_id: int, analyses: list[dict]) -> str:
        filename = f"analysis_run{run_id}_{_date_stamp()}.xlsx"
        path = os.path.join(self.reports_dir, filename)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Analysis"

        headers = [
            "Ticker", "Rec", "Confidence", "Current Price", "Cost Basis",
            "P&L %", "P&L £", "Shares", "30d Target", "Target Lo", "Target Hi",
            "News Sentiment", "Reasoning", "Catalysts", "Risks", "Worries",
            "90d Outlook",
        ]
        _write_header_row(ws, headers)

        for row_num, a in enumerate(analyses, start=2):
            price = a.get("current_price") or 0
            cost = a.get("cost_basis") or 0
            shares = a.get("shares") or 0
            pnl_pct = ((price - cost) / cost * 100) if cost else 0
            pnl_abs = (price - cost) * shares

            values = [
                a.get("ticker", ""),
                a.get("recommendation", ""),
                a.get("confidence", ""),
                price,
                cost,
                pnl_pct / 100,
                pnl_abs,
                shares,
                a.get("price_target_30d"),
                a.get("price_target_lo"),
                a.get("price_target_hi"),
                a.get("news_sentiment", ""),
                a.get("reasoning", ""),
                "\n".join(a.get("catalysts", [])),
                "\n".join(a.get("risks", [])),
                "\n".join(a.get("worries", [])),
                a.get("outlook_90d", ""),
            ]
            for col, val in enumerate(values, start=1):
                cell = ws.cell(row=row_num, column=col, value=val)
                cell.alignment = Alignment(wrap_text=True, vertical="top")

            # Colour-code recommendation cell
            rec = a.get("recommendation", "")
            if rec in _COLOURS:
                ws.cell(row=row_num, column=2).fill = PatternFill(
                    fill_type="solid", fgColor=_COLOURS[rec]
                )
                ws.cell(row=row_num, column=2).font = Font(color="FFFFFF", bold=True)

            # Format P&L % as percentage
            ws.cell(row=row_num, column=6).number_format = "0.00%"

        # Column widths
        col_widths = [10, 8, 12, 14, 12, 10, 10, 10, 12, 10, 10, 16, 50, 40, 40, 30, 50]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.freeze_panes = "C2"
        wb.save(path)
        logger.info("Excel report saved to %s", path)
        return path

    # ── Plain text ─────────────────────────────────────────────────────────────

    def generate_text(self, run_id: int, analyses: list[dict]) -> str:
        filename = f"analysis_run{run_id}_{_date_stamp()}.txt"
        path = os.path.join(self.reports_dir, filename)
        lines = [
            f"STOCK ANALYSIS REPORT — Run #{run_id}",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 72,
        ]
        for a in analyses:
            price = a.get("current_price") or 0
            cost = a.get("cost_basis") or 0
            shares = a.get("shares") or 0
            pnl_pct = ((price - cost) / cost * 100) if cost else 0
            lines += [
                "",
                f"{'─' * 72}",
                f"  {a.get('ticker', '?')}  [{a.get('recommendation', '?')}]  "
                f"Confidence: {a.get('confidence', '?')}",
                f"{'─' * 72}",
                f"  Price: {price:.2f}  Cost: {cost:.2f}  P&L: {pnl_pct:+.1f}%  "
                f"Shares: {shares}",
                f"  30d Target: {a.get('price_target_30d', '?')}  "
                f"Range: {a.get('price_target_lo', '?')} – {a.get('price_target_hi', '?')}",
                "",
                f"  Reasoning: {a.get('reasoning', '')}",
                "",
                f"  News ({a.get('news_sentiment', '?')}): {a.get('news_summary', '')}",
                "",
                "  Catalysts:",
                *[f"    + {c}" for c in a.get("catalysts", [])],
                "  Risks:",
                *[f"    - {r}" for r in a.get("risks", [])],
                "  Worries:",
                *[f"    ! {w}" for w in a.get("worries", [])],
                "",
                f"  90d Outlook: {a.get('outlook_90d', '')}",
            ]
        lines.append("")
        content = "\n".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Text report saved to %s", path)
        return path


def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


def _write_header_row(ws, headers: list[str]) -> None:
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1A237E")
        cell.alignment = Alignment(horizontal="center", vertical="center")
