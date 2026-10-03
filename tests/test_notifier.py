"""Unit tests for the Discord notification module."""

from unittest.mock import MagicMock, patch
import pytest

from notifier import DiscordNotifier, NotificationResult
from scraper import ScrapedItem


@pytest.fixture
def sample_item() -> ScrapedItem:
    """Fixture returning a sample scraped article."""
    return ScrapedItem(
        id="hn_99999",
        title="Show HN: A New Ultra-Fast Python Web Framework",
        url="https://github.com/example/framework",
        source="Hacker News",
        score=250,
        comments_count=75,
        author="alice_coder",
        published_at="1 hour ago",
    )


def test_build_embed_payload(sample_item: ScrapedItem) -> None:
    """Verify embed structure and fields adhere to Discord API standards."""
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    payload = notifier.build_embed_payload(sample_item)

    assert "embeds" in payload
    assert len(payload["embeds"]) == 1

    embed = payload["embeds"][0]
    assert embed["title"] == sample_item.title
    assert embed["url"] == sample_item.url
    # For high score (>100), expect emerald green color
    assert embed["color"] == DiscordNotifier.HIGH_SCORE_COLOR

    field_names = [f["name"] for f in embed["fields"]]
    assert "🔺 Upvotes" in field_names
    assert "💬 Comments" in field_names
    assert "👤 Posted by" in field_names


def test_dry_run_notification(sample_item: ScrapedItem) -> None:
    """Ensure DRY_RUN does not make outbound network requests."""
    notifier = DiscordNotifier(webhook_url="", dry_run=True)
    result = notifier.send_notification(sample_item)

    assert result.success is True
    assert result.status_code == 200


@patch("httpx.Client.post")
def test_send_notification_success(mock_post: MagicMock, sample_item: ScrapedItem) -> None:
    """Test successful webhook dispatch."""
    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_post.return_value = mock_response

    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        rate_limit_delay_seconds=0.0,
    )
    result = notifier.send_notification(sample_item)

    assert result.success is True
    assert result.status_code == 204
    mock_post.assert_called_once()


@patch("httpx.Client.post")
def test_send_notification_rate_limiting_retry(mock_post: MagicMock, sample_item: ScrapedItem) -> None:
    """Test handling of HTTP 429 Rate Limiting with retry."""
    # First response: 429 Rate Limit; Second response: 204 Success
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.json.return_value = {"retry_after": 0.05}

    mock_resp_204 = MagicMock()
    mock_resp_204.status_code = 204

    mock_post.side_effect = [mock_resp_429, mock_resp_204]

    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        rate_limit_delay_seconds=0.0,
    )
    result = notifier.send_notification(sample_item)

    assert result.success is True
    assert result.status_code == 204
    assert mock_post.call_count == 2
