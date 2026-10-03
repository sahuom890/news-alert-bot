"""Storage module for deduplication and persistence using SQLite.

Manages seen article records, alerts dispatch state, and automatic pruning of stale entries.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ItemProtocol(Protocol):
    """Protocol defining the structural interface required by Storage."""

    id: str
    url: str
    title: str
    source: str
    score: int
    comments_count: int


class SQLiteStorage:
    """SQLite-backed persistent deduplication store with WAL mode."""

    def __init__(self, db_path: str | Path = "alerts.db") -> None:
        self.db_path = Path(db_path)
        self._ensure_db_dir()
        self._init_db()

    def _ensure_db_dir(self) -> None:
        """Create parent directory for database file if it doesn't exist."""
        if self.db_path.parent and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Establish a SQLite connection configured with WAL mode and row factory."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging for better concurrency and crash resilience
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        """Initialize database schema and indexes."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_articles (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    score INTEGER DEFAULT 0,
                    comments_count INTEGER DEFAULT 0,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    alert_sent_at TIMESTAMP,
                    alert_status TEXT DEFAULT 'PENDING'
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_first_seen_at 
                ON seen_articles(first_seen_at);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_url 
                ON seen_articles(url);
                """
            )
            conn.commit()
            logger.debug("Database initialized at '%s'", self.db_path)

    def is_seen(self, item_id: str) -> bool:
        """Check whether a single item ID has already been processed.

        Args:
            item_id: Unique identifier for the item.

        Returns:
            True if the item exists in the database, False otherwise.
        """
        query = "SELECT 1 FROM seen_articles WHERE id = ? LIMIT 1;"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (item_id,))
            return cursor.fetchone() is not None

    def filter_unseen_ids(self, item_ids: list[str]) -> set[str]:
        """Given a list of item IDs, return only those that have NOT yet been seen.

        Args:
            item_ids: List of candidate item IDs.

        Returns:
            Set of item IDs that do not exist in the database.
        """
        if not item_ids:
            return set()

        placeholders = ",".join(["?"] * len(item_ids))
        query = f"SELECT id FROM seen_articles WHERE id IN ({placeholders});"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, item_ids)
            seen_ids = {row["id"] for row in cursor.fetchall()}

        return set(item_ids) - seen_ids

    def save_item(
        self,
        item: ItemProtocol,
        alert_status: str = "SENT",
        alert_sent_at: datetime | None = None,
    ) -> bool:
        """Insert or update a scraped item record in the database.

        Args:
            item: Scraped item conforming to ItemProtocol.
            alert_status: Status of the notification ('SENT', 'FAILED', 'SKIPPED').
            alert_sent_at: Timestamp when the alert was dispatched.

        Returns:
            True if record was inserted/updated successfully, False on error.
        """
        now = alert_sent_at or datetime.now(timezone.utc)
        query = """
            INSERT INTO seen_articles (
                id, url, title, source, score, comments_count, first_seen_at, alert_sent_at, alert_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                score = excluded.score,
                comments_count = excluded.comments_count,
                alert_status = excluded.alert_status,
                alert_sent_at = COALESCE(seen_articles.alert_sent_at, excluded.alert_sent_at);
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    query,
                    (
                        item.id,
                        item.url,
                        item.title,
                        item.source,
                        item.score,
                        item.comments_count,
                        now,
                        now if alert_status == "SENT" else None,
                        alert_status,
                    ),
                )
                conn.commit()
                return True
        except sqlite3.Error as exc:
            logger.error("Failed to save item '%s' to database: %s", item.id, exc)
            return False

    def save_items_batch(
        self,
        items: list[ItemProtocol],
        alert_status: str = "SENT",
    ) -> int:
        """Insert a batch of items into the database.

        Args:
            items: List of scraped items.
            alert_status: Status to assign to the batch.

        Returns:
            Number of items successfully stored.
        """
        if not items:
            return 0

        now = datetime.now(timezone.utc)
        records = [
            (
                item.id,
                item.url,
                item.title,
                item.source,
                item.score,
                item.comments_count,
                now,
                now if alert_status == "SENT" else None,
                alert_status,
            )
            for item in items
        ]

        query = """
            INSERT INTO seen_articles (
                id, url, title, source, score, comments_count, first_seen_at, alert_sent_at, alert_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                score = excluded.score,
                comments_count = excluded.comments_count,
                alert_status = excluded.alert_status,
                alert_sent_at = COALESCE(seen_articles.alert_sent_at, excluded.alert_sent_at);
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(query, records)
                conn.commit()
                return cursor.rowcount
        except sqlite3.Error as exc:
            logger.error("Failed to batch save items: %s", exc)
            return 0

    def prune_old_records(self, retention_days: int = 30) -> int:
        """Delete records older than retention_days to keep database size bounded.

        Args:
            retention_days: Number of days to retain records.

        Returns:
            Number of deleted records.
        """
        query = """
            DELETE FROM seen_articles 
            WHERE first_seen_at < datetime('now', '-' || ? || ' days');
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (retention_days,))
                deleted_count = cursor.rowcount
                conn.commit()
                if deleted_count > 0:
                    logger.info(
                        "Pruned %d stale records older than %d days.",
                        deleted_count,
                        retention_days,
                    )
                return deleted_count
        except sqlite3.Error as exc:
            logger.error("Failed to prune old records: %s", exc)
            return 0

    def get_recent_articles(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent articles with alert status and metadata for display."""
        query = """
            SELECT id, url, title, source, score, comments_count, first_seen_at, alert_sent_at, alert_status
            FROM seen_articles
            ORDER BY first_seen_at DESC
            LIMIT ?;
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (limit,))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.error("Failed to fetch recent articles: %s", exc)
            return []

    def get_stats(self) -> dict[str, Any]:
        """Retrieve total and recent article statistics from the database."""
        query = """
            SELECT 
                COUNT(*) as total_tracked,
                SUM(CASE WHEN alert_status = 'SENT' THEN 1 ELSE 0 END) as alerts_sent,
                SUM(CASE WHEN first_seen_at >= datetime('now', '-24 hours') THEN 1 ELSE 0 END) as tracked_last_24h
            FROM seen_articles;
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query)
                row = cursor.fetchone()
                if row:
                    return {
                        "total_tracked": row["total_tracked"] or 0,
                        "alerts_sent": row["alerts_sent"] or 0,
                        "tracked_last_24h": row["tracked_last_24h"] or 0,
                    }
        except sqlite3.Error as exc:
            logger.error("Failed to fetch storage stats: %s", exc)

        return {"total_tracked": 0, "alerts_sent": 0, "tracked_last_24h": 0}
