"""Unit tests for configuration loading and validation."""

import os
import pytest
from config import AppConfig, get_config


def test_default_config() -> None:
    """Verify default configuration values."""
    config = AppConfig(
        DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/123/xyz",
        _env_file=None,
    )
    assert config.TARGET_URL == "https://news.ycombinator.com/news"
    assert config.POLL_INTERVAL_SECONDS == 300
    assert config.MAX_ITEMS_PER_RUN == 15
    assert config.DATABASE_PATH == "alerts.db"
    assert config.LOG_LEVEL == "INFO"
    assert config.DRY_RUN is False


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test environment variable overriding."""
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "600")
    monkeypatch.setenv("MIN_POINTS_THRESHOLD", "100")
    monkeypatch.setenv("DRY_RUN", "true")

    config = AppConfig(_env_file=None)
    assert config.POLL_INTERVAL_SECONDS == 600
    assert config.MIN_POINTS_THRESHOLD == 100
    assert config.DRY_RUN is True
