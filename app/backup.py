"""
Backup manager — copies the SQLite database and reports directory to a
separate backup destination on a schedule or on demand.

Designed for setups where the primary data lives on a fast ZFS pool (M.2)
and backups go to the main Unraid array (parity-protected spinning drives)
or a network mount on Proxmox.

SQLite is backed up via its own hot-backup API (safe with WAL mode, no
file-copy corruption risk). Reports are hard-linked where possible so they
consume no extra space unless the backup filesystem differs from the source.
"""
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_BACKUP_PREFIX = "backup_"


class BackupManager:
    def __init__(
        self,
        db_path: str,
        reports_dir: str,
        backup_path: str,
        retain_days: int = 60,
    ):
        self.db_path      = db_path
        self.reports_dir  = reports_dir
        self.backup_path  = backup_path
        self.retain_days  = retain_days

    def is_configured(self) -> bool:
        return bool(self.backup_path)

    def destination_reachable(self) -> bool:
        if not self.backup_path:
            return False
        try:
            os.makedirs(self.backup_path, exist_ok=True)
            return os.path.isdir(self.backup_path)
        except OSError:
            return False

    # ── Run ────────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Perform a full backup. Returns a status dict.
        Raises OSError / RuntimeError on hard failures.
        """
        if not self.destination_reachable():
            raise RuntimeError(
                f"Backup destination not reachable: {self.backup_path!r}. "
                "Check that the volume is mounted and BACKUP_PATH is correct."
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest  = os.path.join(self.backup_path, f"{_BACKUP_PREFIX}{stamp}")
        os.makedirs(dest, exist_ok=True)

        # ── Database (hot backup via SQLite API) ───────────────────────────────
        db_dest = os.path.join(dest, "stocks.db")
        _sqlite_backup(self.db_path, db_dest)
        db_size = os.path.getsize(db_dest)

        # ── Reports ────────────────────────────────────────────────────────────
        reports_dest = os.path.join(dest, "reports")
        report_count = 0
        if os.path.isdir(self.reports_dir):
            shutil.copytree(self.reports_dir, reports_dest, dirs_exist_ok=True)
            report_count = sum(1 for _ in os.scandir(reports_dest))

        # ── Rotate old backups ─────────────────────────────────────────────────
        removed = self._rotate()

        logger.info(
            "Backup complete → %s  (db: %d KB, reports: %d files, removed: %d old)",
            dest, db_size // 1024, report_count, removed,
        )
        return {
            "timestamp": stamp,
            "destination": dest,
            "db_size_kb": db_size // 1024,
            "report_count": report_count,
            "old_backups_removed": removed,
        }

    # ── List ───────────────────────────────────────────────────────────────────

    def list_backups(self) -> list[dict]:
        if not self.destination_reachable():
            return []
        entries = []
        for e in os.scandir(self.backup_path):
            if not (e.is_dir() and e.name.startswith(_BACKUP_PREFIX)):
                continue
            stamp = e.name[len(_BACKUP_PREFIX):]
            size  = _dir_size_kb(e.path)
            mtime = datetime.fromtimestamp(e.stat().st_mtime, tz=timezone.utc)
            entries.append({
                "name":       e.name,
                "timestamp":  stamp,
                "created_at": mtime.isoformat(),
                "size_kb":    size,
            })
        entries.sort(key=lambda x: x["timestamp"], reverse=True)
        return entries

    def last_backup_time(self) -> Optional[str]:
        backups = self.list_backups()
        return backups[0]["created_at"] if backups else None

    # ── Rotation ───────────────────────────────────────────────────────────────

    def _rotate(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retain_days)
        removed = 0
        for e in os.scandir(self.backup_path):
            if not (e.is_dir() and e.name.startswith(_BACKUP_PREFIX)):
                continue
            mtime = datetime.fromtimestamp(e.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                try:
                    shutil.rmtree(e.path)
                    removed += 1
                    logger.info("Removed old backup: %s", e.name)
                except OSError as exc:
                    logger.warning("Could not remove %s: %s", e.name, exc)
        return removed


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sqlite_backup(src: str, dest: str) -> None:
    """Hot-backup using SQLite's own backup API — safe under concurrent writes."""
    src_conn  = sqlite3.connect(src)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def _dir_size_kb(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total // 1024
