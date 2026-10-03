"""Main entry point and orchestrator for the Web Scraper and Alert Bot.

Handles scheduling, graceful shutdown signals, pipeline execution, and structured logging.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from threading import Event

from config import AppConfig, get_config
from notifier import DiscordNotifier
from scraper import HackerNewsScraper, ScrapedItem
from storage import SQLiteStorage

# Graceful shutdown event
_shutdown_event = Event()


def setup_logging(log_level_str: str) -> None:
    """Configure structured console logging with ISO timestamps and log levels."""
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # Silence overly verbose external loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def signal_handler(signum: int, frame: object | None) -> None:
    """Handle termination signals (SIGINT, SIGTERM) to initiate clean shutdown."""
    sig_name = signal.Signals(signum).name
    logging.getLogger("main").info("Received signal %s (%d). Initiating graceful shutdown...", sig_name, signum)
    _shutdown_event.set()


class AlertBotPipeline:
    """Coordinates scraping, deduplication, filtering, notification, and storage."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("AlertBotPipeline")
        self.storage = SQLiteStorage(db_path=config.DATABASE_PATH)
        self.scraper = HackerNewsScraper(
            base_url=config.TARGET_URL,
            user_agent=config.USER_AGENT,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
        )
        self.notifier = DiscordNotifier(
            webhook_url=config.DISCORD_WEBHOOK_URL,
            dry_run=config.DRY_RUN,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )

    def run_cycle(self) -> dict[str, int | float]:
        """Execute a single scraping, deduplication, and notification cycle.

        Returns:
            Dictionary containing execution cycle metrics.
        """
        start_time = time.time()
        self.logger.info("=== Starting Scrape Cycle ===")

        # Step 1: Scrape target website
        try:
            items = self.scraper.scrape(max_items=self.config.MAX_ITEMS_PER_RUN)
        except Exception as exc:
            self.logger.error("Scraping failed during this cycle: %s", exc)
            return {"scraped": 0, "new": 0, "sent": 0, "failed": 0, "duration": time.time() - start_time}

        if not items:
            self.logger.info("No items retrieved during this scrape pass.")
            return {"scraped": 0, "new": 0, "sent": 0, "failed": 0, "duration": time.time() - start_time}

        # Step 2: Deduplication via SQLite
        all_ids = [item.id for item in items]
        unseen_ids = self.storage.filter_unseen_ids(all_ids)
        new_items = [item for item in items if item.id in unseen_ids]

        self.logger.info(
            "Scraped %d items (%d new, %d already tracked).",
            len(items),
            len(new_items),
            len(items) - len(new_items),
        )

        if not new_items:
            self.logger.info("No new unseen articles detected.")
            return {
                "scraped": len(items),
                "new": 0,
                "sent": 0,
                "failed": 0,
                "duration": time.time() - start_time,
            }

        # Step 3: Apply filtering criteria (e.g. minimum points threshold)
        items_to_alert: list[ScrapedItem] = []
        items_to_skip: list[ScrapedItem] = []

        for item in new_items:
            if item.score >= self.config.MIN_POINTS_THRESHOLD:
                items_to_alert.append(item)
            else:
                items_to_skip.append(item)

        if items_to_skip:
            self.logger.info(
                "Skipping alerts for %d items below score threshold of %d points.",
                len(items_to_skip),
                self.config.MIN_POINTS_THRESHOLD,
            )
            # Store skipped items so we don't evaluate them repeatedly
            self.storage.save_items_batch(items_to_skip, alert_status="SKIPPED")

        # Step 4: Dispatch notifications and record state
        sent_count = 0
        failed_count = 0

        for item in items_to_alert:
            if _shutdown_event.is_set():
                self.logger.warning("Shutdown event active. Aborting remaining alerts.")
                break

            result = self.notifier.send_notification(item)
            if result.success:
                sent_count += 1
                self.storage.save_item(item, alert_status="SENT", alert_sent_at=datetime.now(timezone.utc))
            else:
                failed_count += 1
                self.storage.save_item(item, alert_status="FAILED")

        # Step 5: Prune old records periodically
        self.storage.prune_old_records(retention_days=self.config.CLEANUP_RETENTION_DAYS)

        duration = time.time() - start_time
        self.logger.info(
            "Cycle completed in %.2fs. (Scraped: %d, New: %d, Sent: %d, Failed: %d)",
            duration,
            len(items),
            len(new_items),
            sent_count,
            failed_count,
        )

        return {
            "scraped": len(items),
            "new": len(new_items),
            "sent": sent_count,
            "failed": failed_count,
            "duration": duration,
        }

    def run_daemon(self) -> None:
        """Run continuous monitoring loop until termination signal is received."""
        self.logger.info(
            "Starting daemon loop with polling interval of %d seconds.",
            self.config.POLL_INTERVAL_SECONDS,
        )

        while not _shutdown_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                self.logger.exception("Unhandled error in pipeline cycle: %s", exc)

            self.logger.debug(
                "Waiting %d seconds for next cycle (Press Ctrl+C to stop)...",
                self.config.POLL_INTERVAL_SECONDS,
            )

            # Responsive wait loop using Event
            if _shutdown_event.wait(timeout=self.config.POLL_INTERVAL_SECONDS):
                break

        self.logger.info("Daemon gracefully stopped.")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Production Web Scraper and Discord Alert Bot.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the interactive Web Dashboard & REST API on localhost.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the Web Dashboard server on.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scrape and alert pass, then exit immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate alerts without sending real webhook HTTP requests.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Display SQLite storage metrics and exit.",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=None,
        help="Override the target URL to scrape.",
    )
    parser.add_argument(
        "--min-points",
        type=int,
        default=None,
        help="Override minimum points threshold.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override logging level.",
    )
    return parser.parse_args()


def main() -> None:
    """Application entry point."""
    # Register OS signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args = parse_args()
    config = get_config()

    # Apply CLI overrides
    if args.dry_run:
        config.DRY_RUN = True
    if args.target_url:
        config.TARGET_URL = args.target_url
    if args.min_points is not None:
        config.MIN_POINTS_THRESHOLD = args.min_points
    if args.log_level:
        config.LOG_LEVEL = args.log_level

    setup_logging(config.LOG_LEVEL)
    logger = logging.getLogger("main")

    logger.info("Initializing Web Scraper & Alert Bot...")
    logger.info("Target URL: %s", config.TARGET_URL)
    logger.info("Dry Run Mode: %s", config.DRY_RUN)
    logger.info("Database Path: %s", config.DATABASE_PATH)
    logger.info("Score Threshold: %d points", config.MIN_POINTS_THRESHOLD)

    pipeline = AlertBotPipeline(config)

    # Handle stats command
    if args.stats:
        stats = pipeline.storage.get_stats()
        print("\n=== Storage Statistics ===")
        print(f"Total Tracked Articles: {stats['total_tracked']}")
        print(f"Total Alerts Dispatched: {stats['alerts_sent']}")
        print(f"Tracked in Last 24 Hours: {stats['tracked_last_24h']}")
        print("==========================\n")
        return

    # Check webhook configuration if not in dry-run
    if not config.DRY_RUN and not config.DISCORD_WEBHOOK_URL:
        logger.warning(
            "DISCORD_WEBHOOK_URL is not set. Running in dry-run simulation mode automatically."
        )
        config.DRY_RUN = True
        pipeline.notifier.dry_run = True

    if args.web:
        from app import start_server
        start_server(port=args.port)
        return

    if args.once:
        logger.info("Running in single-pass (--once) mode.")
        pipeline.run_cycle()
    else:
        pipeline.run_daemon()


if __name__ == "__main__":
    main()
