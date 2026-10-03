"""Notification module for dispatching formatted alerts to Discord Webhook.

Constructs rich Discord embeds and handles HTTP POST requests with rate limit
management, retries, and dry-run capabilities.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from scraper import ScrapedItem

logger = logging.getLogger(__name__)


@dataclass
class NotificationResult:
    """Outcome of an alert dispatch operation."""

    success: bool
    status_code: int | None = None
    error_message: str | None = None


class DiscordNotifier:
    """Dispatches rich article alerts to a Discord Webhook endpoint."""

    # Default color: Hacker News vibrant orange
    DEFAULT_EMBED_COLOR: int = 0xFF6600
    HIGH_SCORE_COLOR: int = 0x2ECC71  # Emerald green for trending stories (> 100 points)

    def __init__(
        self,
        webhook_url: str,
        dry_run: bool = False,
        timeout: float = 10.0,
        rate_limit_delay_seconds: float = 0.5,
    ) -> None:
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self.timeout = timeout
        self.rate_limit_delay_seconds = rate_limit_delay_seconds

    def build_embed_payload(self, item: ScrapedItem) -> dict[str, Any]:
        """Construct a Discord Webhook payload with rich embed formatting.

        Args:
            item: Scraped article metadata.

        Returns:
            JSON-serializable dictionary compliant with Discord Webhook API.
        """
        # Dynamic color based on upvotes / engagement
        color = self.HIGH_SCORE_COLOR if item.score >= 100 else self.DEFAULT_EMBED_COLOR

        fields = [
            {"name": "🔺 Upvotes", "value": f"**{item.score}** points", "inline": True},
            {"name": "💬 Comments", "value": f"**{item.comments_count}**", "inline": True},
        ]

        if item.author:
            fields.append({"name": "👤 Posted by", "value": f"`{item.author}`", "inline": True})

        if item.published_at:
            fields.append({"name": "⏱ Posted", "value": item.published_at, "inline": True})

        embed = {
            "title": item.title[:256],  # Discord title limit is 256 chars
            "url": item.url,
            "description": f"New trending article detected on **{item.source}**.",
            "color": color,
            "fields": fields,
            "footer": {
                "text": "News Alert Bot • Real-time Pipeline",
                "icon_url": "https://news.ycombinator.com/favicon.ico",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return {
            "username": "News Alert Bot",
            "avatar_url": "https://news.ycombinator.com/favicon.ico",
            "embeds": [embed],
        }

    def send_notification(self, item: ScrapedItem) -> NotificationResult:
        """Send a single article alert to Discord Webhook.

        Args:
            item: Scraped item to format and dispatch.

        Returns:
            NotificationResult describing the operation outcome.
        """
        payload = self.build_embed_payload(item)

        if self.dry_run or not self.webhook_url:
            logger.info(
                "[DRY RUN / NO WEBHOOK] Alert generated for '%s':\n%s",
                item.title,
                json.dumps(payload, indent=2),
            )
            return NotificationResult(success=True, status_code=200)

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        self.webhook_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )

                    # Discord Rate Limiting handler (HTTP 429)
                    if response.status_code == 429:
                        try:
                            rate_limit_info = response.json()
                            retry_after = float(rate_limit_info.get("retry_after", 1.0))
                        except Exception:
                            retry_after = float(response.headers.get("Retry-After", 1.0))

                        logger.warning(
                            "Discord rate limit encountered. Backing off for %.2fs...",
                            retry_after,
                        )
                        time.sleep(retry_after)
                        continue

                    # Successful webhook delivery
                    if response.status_code in (200, 204):
                        logger.info("Successfully dispatched Discord alert for: '%s'", item.title)
                        # Respectful pacing between alerts
                        time.sleep(self.rate_limit_delay_seconds)
                        return NotificationResult(success=True, status_code=response.status_code)

                    # HTTP Error responses
                    logger.error(
                        "Discord returned HTTP %d for '%s': %s",
                        response.status_code,
                        item.title,
                        response.text,
                    )
                    return NotificationResult(
                        success=False,
                        status_code=response.status_code,
                        error_message=response.text,
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning(
                    "Network error sending alert (Attempt %d/%d): %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt == max_attempts:
                    return NotificationResult(
                        success=False,
                        error_message=f"Network error after {max_attempts} attempts: {exc}",
                    )
                time.sleep(1.5 * attempt)
            except Exception as exc:
                logger.exception("Unexpected error while sending alert for '%s': %s", item.title, exc)
                return NotificationResult(success=False, error_message=str(exc))

        return NotificationResult(success=False, error_message="Exhausted retry attempts.")

    def send_batch(self, items: list[ScrapedItem]) -> tuple[int, int]:
        """Dispatch alerts for a batch of items.

        Args:
            items: List of scraped items to alert.

        Returns:
            Tuple of (successful_alerts_count, failed_alerts_count).
        """
        success_count = 0
        failed_count = 0

        for item in items:
            result = self.send_notification(item)
            if result.success:
                success_count += 1
            else:
                failed_count += 1

        return success_count, failed_count
