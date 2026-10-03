"""Web scraper module for extracting articles and posts.

Uses httpx for resilient HTTP requests with exponential backoff and BeautifulSoup4
for robust, defensive DOM parsing.
"""

from __future__ import annotations

import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


@dataclass
class ScrapedItem:
    """Standardized data representation of a scraped article or post."""

    id: str
    title: str
    url: str
    source: str
    score: int = 0
    comments_count: int = 0
    author: str | None = None
    published_at: str | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert the dataclass instance to a dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "score": self.score,
            "comments_count": self.comments_count,
            "author": self.author,
            "published_at": self.published_at,
            "scraped_at": self.scraped_at.isoformat(),
        }


class BaseScraper(ABC):
    """Abstract base class for news and feed scrapers."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _get_client_headers(self) -> dict[str, str]:
        """Generate HTTP headers with custom User-Agent and acceptance criteria."""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

    def fetch_page(self, url: str | None = None) -> str:
        """Fetch raw HTML content from target URL with exponential backoff and retry logic.

        Args:
            url: URL to fetch (defaults to self.base_url if None).

        Returns:
            Decoded HTML response body as string.

        Raises:
            httpx.HTTPError: If request fails after all retries are exhausted.
        """
        target = url or self.base_url
        headers = self._get_client_headers()

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("Fetching '%s' (Attempt %d/%d)...", target, attempt, self.max_retries)
                with httpx.Client(
                    headers=headers,
                    timeout=self.timeout,
                    follow_redirects=True,
                ) as client:
                    response = client.get(target)

                    # Handle Rate Limiting (429) specifically
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After", 5.0))
                        logger.warning(
                            "HTTP 429 Too Many Requests received. Waiting %.1f seconds before retry...",
                            retry_after,
                        )
                        time.sleep(retry_after)
                        continue

                    response.raise_for_status()
                    return response.text

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                is_last_attempt = attempt == self.max_retries
                status_code = getattr(getattr(exc, "response", None), "status_code", None)

                # Do not retry on client-side 4xx errors (except 429)
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    logger.error("Client error %d for URL '%s': %s", status_code, target, exc)
                    raise

                if is_last_attempt:
                    logger.error("Failed to fetch '%s' after %d attempts: %s", target, attempt, exc)
                    raise

                # Calculate exponential backoff with jitter
                delay = (self.backoff_factor ** (attempt - 1)) + random.uniform(0.1, 0.8)
                logger.warning(
                    "Error fetching '%s' on attempt %d (%s). Retrying in %.2fs...",
                    target,
                    attempt,
                    exc.__class__.__name__,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Unexpected termination while fetching {target}")

    @abstractmethod
    def parse(self, html_content: str, max_items: int = 30) -> list[ScrapedItem]:
        """Parse raw HTML content and extract a list of ScrapedItem instances."""
        raise NotImplementedError


class HackerNewsScraper(BaseScraper):
    """Production scraper for Hacker News frontpage and newest feeds."""

    def __init__(
        self,
        base_url: str = "https://news.ycombinator.com/news",
        user_agent: str = "NewsAlertBot/1.0",
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            user_agent=user_agent,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )

    def scrape(self, max_items: int = 30) -> list[ScrapedItem]:
        """Fetch and parse Hacker News stories."""
        html = self.fetch_page()
        return self.parse(html, max_items=max_items)

    def parse(self, html_content: str, max_items: int = 30) -> list[ScrapedItem]:
        """Safely parse Hacker News HTML markup into ScrapedItem objects.

        Args:
            html_content: Raw HTML text of Hacker News.
            max_items: Maximum number of articles to return.

        Returns:
            List of parsed ScrapedItem objects.
        """
        items: list[ScrapedItem] = []
        if not html_content:
            return items

        soup = BeautifulSoup(html_content, "html.parser")
        story_rows = soup.select("tr.athing")

        for row in story_rows:
            if len(items) >= max_items:
                break

            try:
                item = self._parse_story_row(row)
                if item:
                    items.append(item)
            except Exception as exc:
                # Catch per-row exceptions so malformed items do not abort the entire scrape
                item_id = row.get("id", "unknown")
                logger.warning("Error parsing item row id='%s': %s", item_id, exc)

        logger.info("Successfully scraped %d items from Hacker News.", len(items))
        return items

    def _parse_story_row(self, row: Tag) -> ScrapedItem | None:
        """Parse an individual story row and its accompanying subtext row."""
        item_id = row.get("id")
        if not item_id or not isinstance(item_id, str):
            return None

        # Title and URL extraction
        title_tag = row.select_one("span.titleline > a")
        if not title_tag:
            return None

        title = title_tag.get_text(strip=True)
        raw_url = title_tag.get("href", "")
        if not raw_url:
            raw_url = f"item?id={item_id}"

        # Resolve relative URLs (e.g. Ask HN, Show HN internal discussion links)
        url = urljoin(self.base_url, str(raw_url))

        # Companion subtext row (contains points, author, comments)
        subtext_row = row.find_next_sibling("tr")
        score = 0
        comments_count = 0
        author: str | None = None
        published_at: str | None = None

        if subtext_row and isinstance(subtext_row, Tag):
            # Points / Upvotes
            score_tag = subtext_row.select_one("span.score")
            if score_tag:
                score_match = re.search(r"(\d+)\s+point", score_tag.get_text())
                if score_match:
                    score = int(score_match.group(1))

            # Author
            author_tag = subtext_row.select_one("a.hnuser")
            if author_tag:
                author = author_tag.get_text(strip=True)

            # Age / Published timestamp
            age_tag = subtext_row.select_one("span.age")
            if age_tag:
                published_at = age_tag.get_text(strip=True)

            # Comments count
            # Comments link is usually the last link in the subtext containing 'comment' or 'discuss'
            comment_tags = subtext_row.select("span.subline a, td.subtext a")
            for link in comment_tags:
                link_text = link.get_text(strip=True)
                if "comment" in link_text:
                    comment_match = re.search(r"(\d+)\s+comment", link_text)
                    if comment_match:
                        comments_count = int(comment_match.group(1))
                        break
                elif link_text == "discuss":
                    comments_count = 0

        return ScrapedItem(
            id=f"hn_{item_id}",
            title=title,
            url=url,
            source="Hacker News",
            score=score,
            comments_count=comments_count,
            author=author,
            published_at=published_at,
        )
