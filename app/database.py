import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")   # wait up to 5s for write locks
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at  TEXT NOT NULL,
                    finished_at TEXT,
                    status      TEXT NOT NULL DEFAULT 'running',
                    ticker_count INTEGER DEFAULT 0,
                    log         TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS analyses (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id              INTEGER NOT NULL REFERENCES runs(id),
                    ticker              TEXT NOT NULL,
                    recommendation      TEXT,
                    price_target        REAL,
                    price_target_lo     REAL,
                    price_target_hi     REAL,
                    confidence          TEXT,
                    reasoning           TEXT,
                    news_sentiment      TEXT,
                    news_summary        TEXT,
                    catalysts           TEXT,   -- JSON array
                    risks               TEXT,   -- JSON array
                    worries             TEXT,   -- JSON array
                    outlook_90d         TEXT,
                    current_price       REAL,
                    cost_basis          REAL,
                    shares              REAL,
                    pe_ratio            REAL,
                    eps_growth_pct      REAL,
                    analyst_target_mean REAL,
                    analyst_consensus   TEXT,
                    next_earnings       TEXT,
                    created_at          TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS handoff_notes (
                    ticker              TEXT PRIMARY KEY,
                    thesis_summary      TEXT,
                    watch_items         TEXT,   -- JSON array
                    trend_flags         TEXT,   -- JSON array
                    ongoing_risks       TEXT,   -- JSON array
                    ongoing_catalysts   TEXT,   -- JSON array
                    updated_at          TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    t212_order_id   TEXT UNIQUE,
                    ticker          TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    quantity        REAL NOT NULL,
                    price           REAL NOT NULL,
                    total_value     REAL NOT NULL,
                    traded_at       TEXT NOT NULL,
                    synced_at       TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    ticker          TEXT PRIMARY KEY,
                    shares          REAL NOT NULL,
                    avg_cost        REAL NOT NULL,
                    cost_method     TEXT NOT NULL DEFAULT 'AVCO',
                    source          TEXT NOT NULL DEFAULT 'manual',
                    first_bought    TEXT,
                    last_updated    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owned_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker          TEXT NOT NULL,
                    shares_peak     REAL,
                    avg_cost        REAL,
                    first_bought    TEXT,
                    fully_sold_at   TEXT,
                    realised_pl     REAL,
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS dividends (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    t212_ref        TEXT UNIQUE,    -- T212's reference ID, for dedup
                    ticker          TEXT NOT NULL,
                    amount          REAL NOT NULL,  -- cash received, account currency
                    shares_held     REAL,           -- shares at ex-date (for yield calc)
                    paid_at         TEXT NOT NULL,  -- ISO timestamp
                    synced_at       TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pies (
                    id              INTEGER PRIMARY KEY,
                    name            TEXT NOT NULL,
                    cash            REAL,
                    reinvested      REAL,
                    invested_value  REAL,
                    current_value   REAL,
                    icon            TEXT,
                    synced_at       TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pie_instruments (
                    pie_id      INTEGER NOT NULL REFERENCES pies(id) ON DELETE CASCADE,
                    ticker      TEXT NOT NULL,
                    quantity    REAL NOT NULL,
                    value       REAL,
                    PRIMARY KEY (pie_id, ticker)
                );

                CREATE INDEX IF NOT EXISTS idx_pie_instruments_ticker ON pie_instruments(ticker);

                CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(run_id);
                CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);
                CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
                CREATE INDEX IF NOT EXISTS idx_dividends_ticker ON dividends(ticker);
            """)

    # ── Runs ───────────────────────────────────────────────────────────────────

    def create_run(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
                (_now(),),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, ticker_count: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, ticker_count=? WHERE id=?",
                (_now(), status, ticker_count, run_id),
            )

    def append_log(self, run_id: int, line: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET log = log || ? WHERE id=?",
                (line + "\n", run_id),
            )

    def get_run(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Analyses ───────────────────────────────────────────────────────────────

    def save_analysis(self, run_id: int, ticker: str, result: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO analyses
                   (run_id, ticker, recommendation, price_target, price_target_lo,
                    price_target_hi, confidence, reasoning, news_sentiment,
                    news_summary, catalysts, risks, worries, outlook_90d,
                    current_price, cost_basis, shares,
                    pe_ratio, eps_growth_pct, analyst_target_mean,
                    analyst_consensus, next_earnings, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, ticker,
                    result.get("recommendation"),
                    result.get("price_target_30d"),
                    result.get("price_target_range", [None, None])[0],
                    result.get("price_target_range", [None, None])[1],
                    result.get("confidence"),
                    result.get("reasoning"),
                    result.get("news_sentiment"),
                    result.get("news_summary"),
                    json.dumps(result.get("catalysts", [])),
                    json.dumps(result.get("risks", [])),
                    json.dumps(result.get("worries", [])),
                    result.get("outlook_90d"),
                    result.get("current_price"),
                    result.get("cost_basis"),
                    result.get("shares"),
                    result.get("pe_ratio"),
                    result.get("eps_growth_pct"),
                    result.get("analyst_target_mean"),
                    result.get("analyst_consensus"),
                    result.get("next_earnings"),
                    _now(),
                ),
            )

    def get_analyses_for_run(self, run_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM analyses WHERE run_id=? ORDER BY ticker", (run_id,)
            ).fetchall()
            return [self._decode_analysis(dict(r)) for r in rows]

    def get_ticker_history(self, ticker: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT a.*, r.started_at as run_date FROM analyses a
                   JOIN runs r ON r.id = a.run_id
                   WHERE a.ticker=? ORDER BY a.id DESC LIMIT ?""",
                (ticker, limit),
            ).fetchall()
            return [self._decode_analysis(dict(r)) for r in rows]

    def get_latest_analyses(self) -> list[dict]:
        """Latest analysis for each ticker across all runs."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT a.* FROM analyses a
                   INNER JOIN (
                       SELECT ticker, MAX(id) as max_id FROM analyses GROUP BY ticker
                   ) latest ON a.id = latest.max_id
                   ORDER BY a.ticker""",
            ).fetchall()
            return [self._decode_analysis(dict(r)) for r in rows]

    @staticmethod
    def _decode_analysis(row: dict) -> dict:
        for field in ("catalysts", "risks", "worries"):
            if isinstance(row.get(field), str):
                try:
                    row[field] = json.loads(row[field])
                except (json.JSONDecodeError, TypeError):
                    row[field] = []
        return row

    # ── Handoff notes ──────────────────────────────────────────────────────────

    def get_handoff_note(self, ticker: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM handoff_notes WHERE ticker=?", (ticker,)
            ).fetchone()
            if not row:
                return None
            note = dict(row)
            for field in ("watch_items", "trend_flags", "ongoing_risks", "ongoing_catalysts"):
                if isinstance(note.get(field), str):
                    try:
                        note[field] = json.loads(note[field])
                    except (json.JSONDecodeError, TypeError):
                        note[field] = []
            return note

    def save_handoff_note(self, ticker: str, note: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO handoff_notes
                   (ticker, thesis_summary, watch_items, trend_flags,
                    ongoing_risks, ongoing_catalysts, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     thesis_summary=excluded.thesis_summary,
                     watch_items=excluded.watch_items,
                     trend_flags=excluded.trend_flags,
                     ongoing_risks=excluded.ongoing_risks,
                     ongoing_catalysts=excluded.ongoing_catalysts,
                     updated_at=excluded.updated_at""",
                (
                    ticker,
                    note.get("thesis_summary", ""),
                    json.dumps(note.get("watch_items", [])),
                    json.dumps(note.get("trend_flags", [])),
                    json.dumps(note.get("ongoing_risks", [])),
                    json.dumps(note.get("ongoing_catalysts", [])),
                    _now(),
                ),
            )

    # ── Positions ──────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY ticker").fetchall()
            return [dict(r) for r in rows]

    def upsert_position(self, ticker: str, shares: float, avg_cost: float,
                        source: str = "manual", first_bought: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO positions (ticker, shares, avg_cost, source, first_bought, last_updated)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     shares=excluded.shares, avg_cost=excluded.avg_cost,
                     source=excluded.source, last_updated=excluded.last_updated""",
                (ticker, shares, avg_cost, source, first_bought, _now()),
            )

    # ── Trades ─────────────────────────────────────────────────────────────────

    def save_trades(self, trades: list[dict]) -> int:
        saved = 0
        with self._conn() as conn:
            for t in trades:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO trades
                           (t212_order_id, ticker, action, quantity, price,
                            total_value, traded_at, synced_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (t["order_id"], t["ticker"], t["action"], t["quantity"],
                         t["price"], t["total_value"], t["traded_at"], _now()),
                    )
                    saved += conn.execute("SELECT changes()").fetchone()[0]
                except sqlite3.IntegrityError:
                    pass
        return saved

    def get_trades(self, ticker: Optional[str] = None, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE ticker=? ORDER BY traded_at DESC LIMIT ?",
                    (ticker, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades ORDER BY traded_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_last_sync_time(self) -> Optional[str]:
        """Last time a T212 sync was run (for UI display)."""
        return self.get_setting("t212_last_synced_at")

    def get_latest_trade_time(self) -> Optional[str]:
        """Most recent traded_at in the DB — used as since= cutoff for T212 API."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(traded_at) as last FROM trades"
            ).fetchone()
            return row["last"] if row and row["last"] else None

    # ── Owned history ──────────────────────────────────────────────────────────

    def save_owned_history(self, record: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO owned_history
                   (ticker, shares_peak, avg_cost, first_bought, fully_sold_at, realised_pl, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    record["ticker"], record.get("shares_peak"),
                    record.get("avg_cost"), record.get("first_bought"),
                    record.get("fully_sold_at"), record.get("realised_pl"),
                    record.get("notes"),
                ),
            )

    def get_owned_history(self, ticker: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM owned_history WHERE ticker=? ORDER BY id DESC",
                    (ticker,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM owned_history ORDER BY fully_sold_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    # ── Dividends ──────────────────────────────────────────────────────────────

    def save_dividends(self, dividends: list[dict]) -> int:
        saved = 0
        with self._conn() as conn:
            for d in dividends:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO dividends
                           (t212_ref, ticker, amount, shares_held, paid_at, synced_at)
                           VALUES (?,?,?,?,?,?)""",
                        (d.get("t212_ref"), d["ticker"], d["amount"],
                         d.get("shares_held"), d["paid_at"], _now()),
                    )
                    saved += conn.execute("SELECT changes()").fetchone()[0]
                except Exception:
                    pass
        return saved

    def get_dividends(self, ticker: Optional[str] = None, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM dividends WHERE ticker=? ORDER BY paid_at DESC LIMIT ?",
                    (ticker, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM dividends ORDER BY paid_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_dividend_summary(self) -> list[dict]:
        """Total dividends received per ticker, all time."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT ticker,
                          SUM(amount)  AS total_received,
                          COUNT(*)     AS payment_count,
                          MAX(paid_at) AS last_paid
                   FROM dividends
                   GROUP BY ticker
                   ORDER BY total_received DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_dividend_stats(self) -> dict:
        """Aggregate dividend stats for dashboard: all-time, last 30d, last 7d."""
        with self._conn() as conn:
            def total(where: str, params=()):
                row = conn.execute(
                    f"SELECT COALESCE(SUM(amount), 0) as t FROM dividends{where}", params
                ).fetchone()
                return round(float(row["t"]), 2)

            return {
                "all_time":  total(""),
                "last_30d":  total(" WHERE paid_at >= date('now', '-30 days')"),
                "last_7d":   total(" WHERE paid_at >= date('now', '-7 days')"),
                "count":     conn.execute("SELECT COUNT(*) as c FROM dividends").fetchone()["c"],
            }

    # ── Pies ───────────────────────────────────────────────────────────────────

    def replace_pies(self, pies: list[dict]) -> None:
        """Replace all pie metadata and instrument memberships."""
        with self._conn() as conn:
            conn.execute("DELETE FROM pie_instruments")
            conn.execute("DELETE FROM pies")
            for pie in pies:
                conn.execute(
                    """INSERT INTO pies
                       (id, name, cash, reinvested, invested_value, current_value, icon, synced_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        pie["id"], pie["name"], pie.get("cash"),
                        pie.get("reinvested"), pie.get("invested_value"),
                        pie.get("current_value"), pie.get("icon"), _now(),
                    ),
                )
                for inst in pie.get("instruments", []):
                    conn.execute(
                        """INSERT INTO pie_instruments (pie_id, ticker, quantity, value)
                           VALUES (?,?,?,?)""",
                        (pie["id"], inst["ticker"], inst["quantity"], inst.get("value")),
                    )

    def get_pies(self) -> list[dict]:
        with self._conn() as conn:
            pies = [dict(r) for r in conn.execute(
                "SELECT * FROM pies ORDER BY name"
            ).fetchall()]
            for pie in pies:
                rows = conn.execute(
                    "SELECT ticker, quantity, value FROM pie_instruments WHERE pie_id=? ORDER BY ticker",
                    (pie["id"],),
                ).fetchall()
                pie["instruments"] = [dict(r) for r in rows]
            return pies

    def get_pie_tickers(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT ticker FROM pie_instruments").fetchall()
            return {r["ticker"] for r in rows}

    def get_capital_metrics(self) -> dict:
        """Cached net deposits / reinvested breakdown from last T212 sync."""
        keys = (
            "capital_net_deposits",
            "capital_holdings_cost",
            "capital_reinvested",
            "capital_synced_at",
        )
        raw = {k: self.get_setting(k) for k in keys}
        return {
            "net_deposits":    float(raw["capital_net_deposits"]) if raw["capital_net_deposits"] else None,
            "holdings_cost":   float(raw["capital_holdings_cost"]) if raw["capital_holdings_cost"] else None,
            "reinvested":      float(raw["capital_reinvested"]) if raw["capital_reinvested"] else None,
            "synced_at":       raw["capital_synced_at"],
        }

    def save_capital_metrics(self, metrics: dict) -> None:
        mapping = {
            "net_deposits":  "capital_net_deposits",
            "holdings_cost": "capital_holdings_cost",
            "reinvested":    "capital_reinvested",
        }
        for field, key in mapping.items():
            val = metrics.get(field)
            if val is not None:
                self.set_setting(key, str(round(float(val), 2)))
        self.set_setting("capital_synced_at", _now())

    def get_all_handoff_notes(self) -> dict:
        """Returns all handoff notes keyed by ticker — single query."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM handoff_notes").fetchall()
        result = {}
        for row in rows:
            note = dict(row)
            for field in ("watch_items", "trend_flags", "ongoing_risks", "ongoing_catalysts"):
                if isinstance(note.get(field), str):
                    try:
                        note[field] = json.loads(note[field])
                    except (json.JSONDecodeError, TypeError):
                        note[field] = []
            result[note["ticker"]] = note
        return result

    # ── Settings (key-value store) ─────────────────────────────────────────────

    def get_setting(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                (key, value, _now()),
            )

    def get_key_ages(self) -> dict:
        """
        Returns days since each API key was last confirmed rotated.
        None means the key has never been marked as rotated in the UI.
        """
        from datetime import datetime, timezone
        result = {}
        for key in ("t212_key_rotated_at", "anthropic_key_rotated_at"):
            val = self.get_setting(key)
            if val:
                try:
                    rotated = datetime.fromisoformat(val)
                    days = (datetime.now(timezone.utc) - rotated).days
                    result[key] = days
                except ValueError:
                    result[key] = None
            else:
                result[key] = None
        return result
