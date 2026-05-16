"""
Report generation: Excel (.xlsx) and plain-text formats.

Encryption: set REPORTS_ENCRYPTION_KEY in your .env to enable.
  - Excel files are password-protected with that key.
  - Text files are Fernet-encrypted (saved as .txt.enc, decryptable via /api/reports/<id>/text).

Rotation: files older than REPORTS_RETENTION_DAYS (default 365) are deleted
automatically at the start of each report generation.
"""
import hashlib
import io
import logging
import os
from base64 import urlsafe_b64encode
from datetime import datetime, timezone, timedelta
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

_COLOURS = {
    "BUY":        "2196F3",
    "SELL":       "F44336",
    "HOLD_LONG":  "4CAF50",
    "HOLD_SHORT": "FF9800",
    "HOLD":       "9E9E9E",
    "WATCH":      "9C27B0",
    "POSITIVE":   "4CAF50",
    "NEUTRAL":    "9E9E9E",
    "NEGATIVE":   "F44336",
}

# Badge text colour — dark text on light badges
_BADGE_TEXT = {
    "HOLD_SHORT": "000000",
    "WATCH":      "FFFFFF",
}


def _derive_fernet_key(password: str) -> bytes:
    """Derive a valid 32-byte Fernet key from an arbitrary password string."""
    digest = hashlib.sha256(password.encode()).digest()
    return urlsafe_b64encode(digest)


class ReportGenerator:
    def __init__(self, reports_dir: Optional[str] = None,
                 encryption_key: Optional[str] = None,
                 retention_days: int = 365):
        self.reports_dir = reports_dir or os.getenv("REPORTS_DIR", "/data/reports")
        self.retention_days = int(os.getenv("REPORTS_RETENTION_DAYS", str(retention_days)))
        raw_key = encryption_key or os.getenv("REPORTS_ENCRYPTION_KEY", "")
        self._enc_key: Optional[str] = raw_key if raw_key else None
        os.makedirs(self.reports_dir, exist_ok=True)

    # ── Excel ──────────────────────────────────────────────────────────────────

    def generate_excel(self, run_id: int, analyses: list[dict]) -> str:
        self._rotate_old_reports()

        filename = f"analysis_run{run_id}_{_date_stamp()}.xlsx"
        path = os.path.join(self.reports_dir, filename)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Analysis"

        headers = [
            "Ticker", "Rec", "Confidence", "Current Price", "Cost Basis",
            "P&L %", "P&L £", "Shares", "30d Target", "Target Lo", "Target Hi",
            "P/E", "EPS Growth %", "Analyst Target", "Analyst Consensus",
            "News Sentiment", "Reasoning", "Catalysts", "Risks", "Worries",
            "90d Outlook",
        ]
        _write_header_row(ws, headers)

        for row_num, a in enumerate(analyses, start=2):
            price  = a.get("current_price") or 0
            cost   = a.get("cost_basis") or 0
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
                a.get("pe_ratio"),
                a.get("eps_growth_pct"),
                a.get("analyst_target_mean"),
                a.get("analyst_consensus", "").upper() if a.get("analyst_consensus") else "",
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

            rec = a.get("recommendation", "")
            if rec in _COLOURS:
                ws.cell(row=row_num, column=2).fill = PatternFill(
                    fill_type="solid", fgColor=_COLOURS[rec]
                )
                text_colour = _BADGE_TEXT.get(rec, "FFFFFF")
                ws.cell(row=row_num, column=2).font = Font(color=text_colour, bold=True)

            ws.cell(row=row_num, column=6).number_format = "0.00%"

        col_widths = [10, 12, 12, 14, 12, 10, 10, 10, 12, 10, 10,
                      8, 12, 14, 16, 16, 50, 40, 40, 30, 50]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.freeze_panes = "C2"
        wb.save(path)

        if self._enc_key:
            path = self._encrypt_excel(path)

        logger.info("Excel report saved to %s", path)
        return path

    # ── Plain text ─────────────────────────────────────────────────────────────

    def generate_text(self, run_id: int, analyses: list[dict]) -> str:
        self._rotate_old_reports()

        ext = "txt.enc" if self._enc_key else "txt"
        filename = f"analysis_run{run_id}_{_date_stamp()}.{ext}"
        path = os.path.join(self.reports_dir, filename)

        lines = [
            f"STOCK ANALYSIS REPORT — Run #{run_id}",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 72,
        ]
        for a in analyses:
            price  = a.get("current_price") or 0
            cost   = a.get("cost_basis") or 0
            shares = a.get("shares") or 0
            pnl_pct = ((price - cost) / cost * 100) if cost else 0
            lines += [
                "",
                "─" * 72,
                f"  {a.get('ticker', '?')}  [{a.get('recommendation', '?')}]  "
                f"Confidence: {a.get('confidence', '?')}",
                "─" * 72,
                f"  Price: {price:.2f}  Cost: {cost:.2f}  P&L: {pnl_pct:+.1f}%  Shares: {shares}",
                f"  30d Target: {a.get('price_target_30d', '?')}  "
                f"Range: {a.get('price_target_lo', '?')} – {a.get('price_target_hi', '?')}",
                "",
            ]
            if a.get("pe_ratio"):
                lines.append(f"  P/E: {a['pe_ratio']}  "
                             f"EPS growth: {a.get('eps_growth_pct', '?')}%  "
                             f"Analyst target: {a.get('analyst_target_mean', '?')} "
                             f"({a.get('analyst_consensus', '').upper()})")
                lines.append("")
            lines += [
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
        content = "\n".join(lines).encode("utf-8")

        if self._enc_key:
            content = self._fernet().encrypt(content)
            with open(path, "wb") as f:
                f.write(content)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.decode("utf-8"))

        logger.info("Text report saved to %s", path)
        return path

    def decrypt_text_report(self, path: str) -> str:
        """Decrypt a .txt.enc file and return its contents as a string."""
        if not self._enc_key:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        with open(path, "rb") as f:
            return self._fernet().decrypt(f.read()).decode("utf-8")

    # ── Rotation ───────────────────────────────────────────────────────────────

    def _rotate_old_reports(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        deleted = 0
        for fname in os.listdir(self.reports_dir):
            fpath = os.path.join(self.reports_dir, fname)
            if not os.path.isfile(fpath):
                continue
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
            if mtime < cutoff:
                try:
                    os.remove(fpath)
                    deleted += 1
                except OSError as exc:
                    logger.warning("Could not delete old report %s: %s", fpath, exc)
        if deleted:
            logger.info("Rotated %d report(s) older than %d days", deleted, self.retention_days)

    # ── Encryption helpers ─────────────────────────────────────────────────────

    def _fernet(self):
        from cryptography.fernet import Fernet
        return Fernet(_derive_fernet_key(self._enc_key))

    def _encrypt_excel(self, path: str) -> str:
        """Password-protect the xlsx in-place using msoffcrypto-tool."""
        try:
            import msoffcrypto
            encrypted_path = path  # overwrite the unencrypted file
            buf = io.BytesIO()
            with open(path, "rb") as f:
                office_file = msoffcrypto.OfficeFile(f)
                office_file.encrypt(self._enc_key, buf)
            with open(encrypted_path, "wb") as f:
                f.write(buf.getvalue())
            return encrypted_path
        except Exception as exc:
            logger.warning("Excel encryption failed (file saved unencrypted): %s", exc)
            return path


# ── Helpers ────────────────────────────────────────────────────────────────────

def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


def _write_header_row(ws, headers: list[str]) -> None:
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1A237E")
        cell.alignment = Alignment(horizontal="center", vertical="center")
