"""Integration tests for the complete AlertBotPipeline execution cycle."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from config import AppConfig
from main import AlertBotPipeline
from scraper import ScrapedItem


@pytest.fixture
def isolated_pipeline(tmp_path: Path) -> AlertBotPipeline:
    """Fixture providing an isolated pipeline backed by a temporary SQLite file."""
    db_file = tmp_path / "integration_alerts.db"
    config = AppConfig(
        DATABASE_PATH=str(db_file),
        DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/dummy/url",
        MIN_POINTS_THRESHOLD=50,
        DRY_RUN=True,
        _env_file=None,
    )
    return AlertBotPipeline(config)


def test_pipeline_cycle_end_to_end(isolated_pipeline: AlertBotPipeline) -> None:
    """Test full cycle execution including scraping, filtering, notifying, and persistence."""
    mock_items = [
        ScrapedItem(
            id="item_high",
            title="High Score Post",
            url="https://example.com/high",
            source="Hacker News",
            score=150,
            comments_count=30,
        ),
        ScrapedItem(
            id="item_low",
            title="Low Score Post",
            url="https://example.com/low",
            source="Hacker News",
            score=10,
            comments_count=2,
        ),
    ]

    with patch.object(isolated_pipeline.scraper, "scrape", return_value=mock_items):
        # Run first cycle
        metrics1 = isolated_pipeline.run_cycle()

        assert metrics1["scraped"] == 2
        assert metrics1["new"] == 2
        assert metrics1["sent"] == 1  # Only item_high satisfies MIN_POINTS_THRESHOLD (>= 50)

        # Verify items are now recorded in SQLite
        assert isolated_pipeline.storage.is_seen("item_high")
        assert isolated_pipeline.storage.is_seen("item_low")

        # Run second cycle with the same items: deduplication should skip both
        metrics2 = isolated_pipeline.run_cycle()
        assert metrics2["scraped"] == 2
        assert metrics2["new"] == 0
        assert metrics2["sent"] == 0
