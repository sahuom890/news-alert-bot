"""Unit tests for the SQLite storage and deduplication module."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import pytest

from scraper import ScrapedItem
from storage import SQLiteStorage


@pytest.fixture
def temp_storage(tmp_path: Path) -> SQLiteStorage:
    """Fixture providing an isolated SQLiteStorage instance."""
    db_file = tmp_path / "test_alerts.db"
    return SQLiteStorage(db_path=db_file)


def test_storage_init_creates_table_and_indexes(temp_storage: SQLiteStorage) -> None:
    """Verify that table and indexes are properly created."""
    conn = temp_storage._get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_articles';")
    assert cursor.fetchone() is not None


def test_is_seen_and_save_item(temp_storage: SQLiteStorage) -> None:
    """Test saving an item and querying seen status."""
    item = ScrapedItem(
        id="hn_12345",
        title="Test Title",
        url="https://example.com/test",
        source="Hacker News",
        score=42,
        comments_count=10,
        author="alice",
    )

    assert not temp_storage.is_seen(item.id)
    assert temp_storage.save_item(item, alert_status="SENT")
    assert temp_storage.is_seen(item.id)


def test_filter_unseen_ids(temp_storage: SQLiteStorage) -> None:
    """Test batch filtering of unseen IDs."""
    item1 = ScrapedItem(id="item_1", title="Title 1", url="https://example.com/1", source="HN")
    item2 = ScrapedItem(id="item_2", title="Title 2", url="https://example.com/2", source="HN")

    temp_storage.save_item(item1)

    unseen = temp_storage.filter_unseen_ids(["item_1", "item_2", "item_3"])
    assert unseen == {"item_2", "item_3"}


def test_save_items_batch(temp_storage: SQLiteStorage) -> None:
    """Test batch insertion of items."""
    items = [
        ScrapedItem(id=f"item_{i}", title=f"Title {i}", url=f"https://example.com/{i}", source="HN")
        for i in range(5)
    ]
    inserted = temp_storage.save_items_batch(items, alert_status="SKIPPED")
    assert inserted == 5

    for item in items:
        assert temp_storage.is_seen(item.id)


def test_prune_old_records(temp_storage: SQLiteStorage) -> None:
    """Test pruning of stale records."""
    # Insert an old record directly
    conn = temp_storage._get_connection()
    conn.execute(
        """
        INSERT INTO seen_articles (id, url, title, source, first_seen_at)
        VALUES ('old_1', 'https://old.com', 'Old', 'HN', datetime('now', '-40 days'));
        """
    )
    conn.commit()

    # Insert a fresh record
    fresh_item = ScrapedItem(id="fresh_1", title="Fresh", url="https://fresh.com", source="HN")
    temp_storage.save_item(fresh_item)

    assert temp_storage.is_seen("old_1")
    assert temp_storage.is_seen("fresh_1")

    deleted = temp_storage.prune_old_records(retention_days=30)
    assert deleted == 1
    assert not temp_storage.is_seen("old_1")
    assert temp_storage.is_seen("fresh_1")
