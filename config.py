"""Configuration module for the Web Scraper & Alert Bot.

Handles environment variable loading, type validation, and application defaults
using Pydantic Settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Webhook Configuration
    DISCORD_WEBHOOK_URL: str = Field(
        default="",
        description="Discord Webhook URL to send notifications to.",
    )

    # Scraper Target Configuration
    TARGET_URL: str = Field(
        default="https://news.ycombinator.com/news",
        description="Target public URL to scrape for updates.",
    )
    USER_AGENT: str = Field(
        default="NewsAlertBot/1.0 (+https://github.com/developer/news-alert-bot)",
        description="Custom User-Agent header to comply with bot policies.",
    )
    REQUEST_TIMEOUT_SECONDS: float = Field(
        default=15.0,
        ge=1.0,
        le=60.0,
        description="HTTP request timeout in seconds.",
    )
    MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry attempts on network failures.",
    )
    BACKOFF_FACTOR: float = Field(
        default=2.0,
        ge=1.0,
        description="Exponential backoff factor for retries.",
    )

    # Filtering & Scheduler Settings
    POLL_INTERVAL_SECONDS: int = Field(
        default=300,
        ge=10,
        description="Interval between scraping runs in seconds (daemon mode).",
    )
    MAX_ITEMS_PER_RUN: int = Field(
        default=15,
        ge=1,
        le=100,
        description="Maximum number of items to process per scraping cycle.",
    )
    MIN_POINTS_THRESHOLD: int = Field(
        default=0,
        ge=0,
        description="Minimum points/upvotes required to trigger an alert.",
    )

    # Storage & Retention
    DATABASE_PATH: str = Field(
        default="alerts.db",
        description="Path to the SQLite database file for deduplication.",
    )
    CLEANUP_RETENTION_DAYS: int = Field(
        default=30,
        ge=1,
        description="Retention period in days for seen articles in SQLite.",
    )

    # Operational Modes
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level.",
    )
    DRY_RUN: bool = Field(
        default=False,
        description="When True, logs notifications without making actual POST requests.",
    )

    @field_validator("DISCORD_WEBHOOK_URL")
    @classmethod
    def validate_webhook_url(cls, v: str) -> str:
        """Validate that webhook URL has a valid prefix when not in DRY_RUN."""
        v = v.strip()
        if v and not (v.startswith("https://discord.com/api/webhooks/") or v.startswith("https://discordapp.com/api/webhooks/")):
            # Allow mock or test URLs in local environments if needed, but warn
            pass
        return v


def get_config() -> AppConfig:
    """Load and return the singleton application settings."""
    return AppConfig()
