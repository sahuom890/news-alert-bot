"""FastAPI Web Application & Real-Time Dashboard for the Scraper & Alert Bot.

Provides a modern local web interface to monitor live feeds, trigger scrape cycles,
preview Discord alerts, and adjust pipeline configurations.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any
import threading
import time

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from config import AppConfig, get_config
from main import AlertBotPipeline
from notifier import DiscordNotifier
from scraper import ScrapedItem
from storage import SQLiteStorage

# In-memory circular buffer for log capture
recent_logs: deque[dict[str, Any]] = deque(maxlen=200)


class MemoryLogHandler(logging.Handler):
    """Custom logging handler to capture application logs for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            recent_logs.append({
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
                "formatted": msg,
            })
        except Exception:
            self.handleError(record)


# Configure loggers
root_logger = logging.getLogger()
mem_handler = MemoryLogHandler()
mem_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"))
root_logger.addHandler(mem_handler)

# Initialize configuration and pipeline
config = get_config()
pipeline = AlertBotPipeline(config)

app = FastAPI(
    title="News Scraper & Alert Bot Dashboard",
    description="Real-time monitoring, manual triggers, and configuration dashboard.",
    version="1.0.0",
)

# Background worker state
_bg_thread: threading.Thread | None = None
_stop_bg_event = threading.Event()
_is_scraping = False
_last_cycle_stats: dict[str, Any] = {
    "last_run": None,
    "scraped": 0,
    "new": 0,
    "sent": 0,
    "failed": 0,
    "duration": 0.0,
}


def background_scrape_worker() -> None:
    """Daemon thread loop executing scheduled scrape cycles."""
    global _is_scraping, _last_cycle_stats
    logging.getLogger("Worker").info("Background daemon worker started.")

    while not _stop_bg_event.is_set():
        try:
            _is_scraping = True
            metrics = pipeline.run_cycle()
            _last_cycle_stats = {
                "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                **metrics,
            }
        except Exception as exc:
            logging.getLogger("Worker").exception("Error during scheduled scrape cycle: %s", exc)
        finally:
            _is_scraping = False

        # Wait for the configured poll interval
        interval = max(10, config.POLL_INTERVAL_SECONDS)
        if _stop_bg_event.wait(timeout=interval):
            break

    logging.getLogger("Worker").info("Background daemon worker stopped.")


@app.on_event("startup")
def startup_event() -> None:
    """Start background scraping worker on application startup."""
    global _bg_thread
    _stop_bg_event.clear()
    _bg_thread = threading.Thread(target=background_scrape_worker, daemon=True, name="ScraperWorker")
    _bg_thread.start()


@app.on_event("shutdown")
def shutdown_event() -> None:
    """Stop background worker on server shutdown."""
    _stop_bg_event.set()
    if _bg_thread and _bg_thread.is_alive():
        _bg_thread.join(timeout=2.0)


class ConfigUpdateRequest(BaseModel):
    """Schema for updating runtime settings from UI."""
    discord_webhook_url: str | None = None
    target_url: str | None = None
    poll_interval_seconds: int | None = None
    min_points_threshold: int | None = None
    dry_run: bool | None = None


# HTML Template for Dashboard
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>News Scraper & Alert Bot Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #0b0f19;
      --bg-secondary: #111827;
      --bg-card: rgba(17, 24, 39, 0.7);
      --border-color: rgba(255, 255, 255, 0.08);
      --text-primary: #f3f4f6;
      --text-secondary: #9ca3af;
      --text-muted: #6b7280;
      --accent-orange: #ff6600;
      --accent-green: #10b981;
      --accent-blue: #3b82f6;
      --accent-purple: #8b5cf6;
      --accent-red: #ef4444;
      --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-primary);
      color: var(--text-primary);
      font-family: var(--font-sans);
      min-height: 100vh;
      padding: 24px;
      line-height: 1.5;
      background-image: 
        radial-gradient(circle at 15% 15%, rgba(255, 102, 0, 0.05) 0%, transparent 40%),
        radial-gradient(circle at 85% 85%, rgba(59, 130, 246, 0.04) 0%, transparent 40%);
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-color);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .logo-icon {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #ff6600, #ff8533);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      font-weight: 800;
      color: white;
      box-shadow: 0 4px 14px rgba(255, 102, 0, 0.35);
    }

    .brand-text h1 {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .brand-text p {
      font-size: 13px;
      color: var(--text-secondary);
    }

    .actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.12);
      color: var(--accent-green);
      border: 1px solid rgba(16, 185, 129, 0.25);
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background-color: var(--accent-green);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }

    .btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 18px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      text-decoration: none;
    }

    .btn-primary {
      background: linear-gradient(135deg, #ff6600, #ea580c);
      color: white;
      box-shadow: 0 4px 14px rgba(255, 102, 0, 0.3);
    }

    .btn-primary:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 18px rgba(255, 102, 0, 0.45);
    }

    .btn-secondary {
      background: var(--bg-secondary);
      color: var(--text-primary);
      border: 1px solid var(--border-color);
    }

    .btn-secondary:hover {
      background: rgba(255, 255, 255, 0.05);
      border-color: rgba(255, 255, 255, 0.2);
    }

    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none !important;
    }

    /* Stats Grid */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
    }

    .stat-card {
      background: var(--bg-card);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s;
    }

    .stat-card:hover {
      border-color: rgba(255, 255, 255, 0.18);
    }

    .stat-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: var(--text-secondary);
      font-size: 13px;
      font-weight: 500;
    }

    .stat-value {
      font-size: 32px;
      font-weight: 800;
      letter-spacing: -0.02em;
    }

    .stat-meta {
      font-size: 12px;
      color: var(--text-muted);
    }

    /* Main Grid */
    .main-grid {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 24px;
    }

    @media (max-width: 1024px) {
      .main-grid {
        grid-template-columns: 1fr;
      }
    }

    /* Card Panels */
    .panel {
      background: var(--bg-card);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    .panel-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(255, 255, 255, 0.02);
    }

    .panel-title {
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .search-box {
      padding: 6px 12px;
      background: var(--bg-primary);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      color: white;
      font-size: 12px;
      outline: none;
      width: 180px;
    }

    .search-box:focus {
      border-color: var(--accent-orange);
    }

    .article-list {
      display: flex;
      flex-direction: column;
      max-height: 620px;
      overflow-y: auto;
    }

    .article-item {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      transition: background 0.15s;
    }

    .article-item:hover {
      background: rgba(255, 255, 255, 0.02);
    }

    .article-info {
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1;
    }

    .article-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-primary);
      text-decoration: none;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .article-title:hover {
      color: var(--accent-orange);
    }

    .article-meta {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      color: var(--text-secondary);
      flex-wrap: wrap;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 600;
    }

    .badge-sent { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); }
    .badge-skipped { background: rgba(107, 114, 128, 0.2); color: var(--text-secondary); }
    .badge-failed { background: rgba(239, 68, 68, 0.15); color: var(--accent-red); }
    .badge-score { background: rgba(255, 102, 0, 0.15); color: var(--accent-orange); }

    /* Side Panel */
    .side-stack {
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* Discord Mockup */
    .discord-mockup {
      background: #313338;
      border-radius: 12px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-family: var(--font-sans);
    }

    .discord-header {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .discord-avatar {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: #ff6600;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 16px;
      color: white;
    }

    .discord-bot-tag {
      background: #5865f2;
      color: white;
      font-size: 10px;
      font-weight: 700;
      padding: 1px 4px;
      border-radius: 3px;
      text-transform: uppercase;
    }

    .discord-embed {
      border-left: 4px solid var(--accent-orange);
      background: #2b2d31;
      border-radius: 4px 8px 8px 4px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .discord-embed-title {
      font-size: 14px;
      font-weight: 700;
      color: #00a8fc;
      text-decoration: none;
    }

    .discord-embed-desc {
      font-size: 12px;
      color: #dbdee1;
    }

    .discord-embed-fields {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 4px;
    }

    .discord-field-name {
      font-size: 11px;
      font-weight: 600;
      color: #949ba4;
    }

    .discord-field-val {
      font-size: 12px;
      font-weight: 600;
      color: #f2f3f5;
    }

    /* Logs Console */
    .logs-console {
      background: #06090e;
      border-radius: 12px;
      padding: 14px;
      font-family: var(--font-mono);
      font-size: 11.5px;
      max-height: 260px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 6px;
      color: #d1d5db;
    }

    .log-line {
      display: flex;
      gap: 8px;
      word-break: break-all;
    }

    .log-time { color: var(--text-muted); }
    .log-INFO { color: #60a5fa; }
    .log-WARNING { color: #fbbf24; }
    .log-ERROR { color: #f87171; }

    /* Configuration Modal/Form */
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-bottom: 12px;
    }

    .form-group label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
    }

    .form-control {
      background: var(--bg-primary);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 8px 12px;
      color: white;
      font-size: 13px;
      outline: none;
    }

    .form-control:focus {
      border-color: var(--accent-orange);
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header>
      <div class="brand">
        <div class="logo-icon">Y</div>
        <div class="brand-text">
          <h1>News Scraper & Alert Bot</h1>
          <p>Real-time Aggregation, Deduplication & Discord Webhook Engine</p>
        </div>
      </div>
      <div class="actions">
        <div class="status-badge" id="statusBadge">
          <div class="status-dot"></div>
          <span id="daemonStatusText">Daemon Active</span>
        </div>
        <button class="btn btn-secondary" onclick="openConfigModal()">⚙️ Settings</button>
        <button class="btn btn-secondary" onclick="testWebhook()" id="btnTestWebhook">🧪 Test Webhook</button>
        <button class="btn btn-primary" onclick="triggerScrape()" id="btnScrape">🚀 Run Scrape Pass</button>
      </div>
    </header>

    <!-- Stats Grid -->
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-header">
          <span>Total Tracked</span>
          <span>📁</span>
        </div>
        <div class="stat-value" id="statTotalTracked">--</div>
        <div class="stat-meta">Unique articles in SQLite WAL</div>
      </div>
      <div class="stat-card">
        <div class="stat-header">
          <span>Alerts Dispatched</span>
          <span>🔔</span>
        </div>
        <div class="stat-value" id="statAlertsSent" style="color: var(--accent-green);">--</div>
        <div class="stat-meta">Delivered to Discord Webhook</div>
      </div>
      <div class="stat-card">
        <div class="stat-header">
          <span>Last 24h Discovered</span>
          <span>📈</span>
        </div>
        <div class="stat-value" id="statTracked24h" style="color: var(--accent-blue);">--</div>
        <div class="stat-meta">Fresh articles indexed today</div>
      </div>
      <div class="stat-card">
        <div class="stat-header">
          <span>Score Threshold</span>
          <span>🎯</span>
        </div>
        <div class="stat-value" id="statThreshold" style="color: var(--accent-orange);">--</div>
        <div class="stat-meta" id="statPollInterval">Polling: --s interval</div>
      </div>
    </div>

    <!-- Main Section -->
    <div class="main-grid">
      <!-- Left Column: Articles Table -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span>📰 Tracked Articles Feed</span>
            <span class="badge badge-sent" id="feedCountBadge">0 items</span>
          </div>
          <input type="text" id="searchInput" class="search-box" placeholder="Filter by title..." oninput="filterArticles()">
        </div>
        <div class="article-list" id="articleList">
          <div style="padding: 30px; text-align: center; color: var(--text-muted);">Loading articles...</div>
        </div>
      </div>

      <!-- Right Column: Discord Preview & Logs -->
      <div class="side-stack">
        <!-- Discord Preview Card -->
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">💬 Discord Embed Preview</div>
            <span class="badge" style="background: rgba(88, 101, 242, 0.2); color: #5865f2;">Live Layout</span>
          </div>
          <div style="padding: 16px;">
            <div class="discord-mockup">
              <div class="discord-header">
                <div class="discord-avatar">Y</div>
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: white;">News Alert Bot</span>
                  <span class="discord-bot-tag">BOT</span>
                  <span style="font-size: 11px; color: #949ba4; margin-left: 6px;">Today at 12:00</span>
                </div>
              </div>
              <div class="discord-embed" id="previewEmbed">
                <a href="#" class="discord-embed-title" id="previewTitle">Building a High-Performance Database from Scratch</a>
                <div class="discord-embed-desc">New trending article detected on <strong>Hacker News</strong>.</div>
                <div class="discord-embed-fields">
                  <div>
                    <div class="discord-field-name">🔺 Upvotes</div>
                    <div class="discord-field-val" id="previewScore">185 points</div>
                  </div>
                  <div>
                    <div class="discord-field-name">💬 Comments</div>
                    <div class="discord-field-val" id="previewComments">42</div>
                  </div>
                  <div>
                    <div class="discord-field-name">👤 Posted by</div>
                    <div class="discord-field-val" id="previewAuthor">alexdev</div>
                  </div>
                  <div>
                    <div class="discord-field-name">⏱ Posted</div>
                    <div class="discord-field-val" id="previewAge">3 hours ago</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Live Application Logs Console -->
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">📟 Live Execution Logs</div>
            <button class="btn btn-secondary" style="padding: 4px 10px; font-size: 11px;" onclick="fetchLogs()">Refresh</button>
          </div>
          <div style="padding: 16px;">
            <div class="logs-console" id="logsConsole">
              <div class="log-line"><span class="log-time">[Init]</span> Loading daemon logs...</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let allArticles = [];

    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        
        document.getElementById('statTotalTracked').innerText = data.storage.total_tracked;
        document.getElementById('statAlertsSent').innerText = data.storage.alerts_sent;
        document.getElementById('statTracked24h').innerText = data.storage.tracked_last_24h;
        document.getElementById('statThreshold').innerText = data.config.min_points_threshold + ' pts';
        document.getElementById('statPollInterval').innerText = `Polling: ${data.config.poll_interval_seconds}s interval`;
      } catch (err) {
        console.error("Error fetching stats:", err);
      }
    }

    async function fetchArticles() {
      try {
        const res = await fetch('/api/articles');
        allArticles = await res.json();
        renderArticles(allArticles);
        if (allArticles.length > 0) {
          updatePreview(allArticles[0]);
        }
      } catch (err) {
        console.error("Error fetching articles:", err);
      }
    }

    function renderArticles(articles) {
      const container = document.getElementById('articleList');
      document.getElementById('feedCountBadge').innerText = `${articles.length} items`;

      if (articles.length === 0) {
        container.innerHTML = '<div style="padding: 40px; text-align: center; color: var(--text-muted);">No articles tracked yet. Click "Run Scrape Pass" to fetch live stories!</div>';
        return;
      }

      container.innerHTML = articles.map(item => {
        let badgeClass = 'badge-skipped';
        if (item.alert_status === 'SENT') badgeClass = 'badge-sent';
        if (item.alert_status === 'FAILED') badgeClass = 'badge-failed';

        return `
          <div class="article-item" onclick='updatePreview(${JSON.stringify(item)})' style="cursor: pointer;">
            <div class="article-info">
              <a href="${item.url}" target="_blank" class="article-title" onclick="event.stopPropagation()">${escapeHtml(item.title)}</a>
              <div class="article-meta">
                <span class="badge badge-score">🔺 ${item.score || 0} pts</span>
                <span>💬 ${item.comments_count || 0} comments</span>
                <span>⏱️ ${item.first_seen_at ? item.first_seen_at.substring(11, 16) : ''} UTC</span>
                <span>🌐 ${item.source}</span>
              </div>
            </div>
            <div>
              <span class="badge ${badgeClass}">${item.alert_status}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function filterArticles() {
      const q = document.getElementById('searchInput').value.toLowerCase();
      const filtered = allArticles.filter(a => a.title.toLowerCase().includes(q));
      renderArticles(filtered);
    }

    function updatePreview(item) {
      document.getElementById('previewTitle').innerText = item.title;
      document.getElementById('previewTitle').href = item.url;
      document.getElementById('previewScore').innerText = `${item.score || 0} points`;
      document.getElementById('previewComments').innerText = `${item.comments_count || 0}`;
      document.getElementById('previewAuthor').innerText = item.author || 'community';
      document.getElementById('previewAge').innerText = 'Recently';
    }

    async function fetchLogs() {
      try {
        const res = await fetch('/api/logs');
        const logs = await res.json();
        const consoleEl = document.getElementById('logsConsole');

        if (logs.length === 0) return;

        consoleEl.innerHTML = logs.slice(-40).map(l => `
          <div class="log-line">
            <span class="log-time">${l.timestamp}</span>
            <span class="log-${l.level}">[${l.level}]</span>
            <span>${escapeHtml(l.message)}</span>
          </div>
        `).join('');

        consoleEl.scrollTop = consoleEl.scrollHeight;
      } catch (err) {
        console.error("Error fetching logs:", err);
      }
    }

    async function triggerScrape() {
      const btn = document.getElementById('btnScrape');
      btn.disabled = true;
      btn.innerText = "⏳ Scraping...";

      try {
        const res = await fetch('/api/scrape', { method: 'POST' });
        const data = await res.json();
        await fetchStats();
        await fetchArticles();
        await fetchLogs();
      } catch (err) {
        alert("Scrape trigger failed: " + err);
      } finally {
        btn.disabled = false;
        btn.innerText = "🚀 Run Scrape Pass";
      }
    }

    async function testWebhook() {
      const btn = document.getElementById('btnTestWebhook');
      btn.disabled = true;
      btn.innerText = "⏳ Testing...";

      try {
        const res = await fetch('/api/test-webhook', { method: 'POST' });
        const data = await res.json();
        alert(data.message || "Webhook test complete!");
        await fetchLogs();
      } catch (err) {
        alert("Webhook test failed: " + err);
      } finally {
        btn.disabled = false;
        btn.innerText = "🧪 Test Webhook";
      }
    }

    function escapeHtml(text) {
      if (!text) return "";
      return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    // Auto-refresh interval
    setInterval(() => {
      fetchStats();
      fetchArticles();
      fetchLogs();
    }, 4000);

    // Initial load
    fetchStats();
    fetchArticles();
    fetchLogs();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def get_dashboard() -> str:
    """Serve the single-page interactive dashboard."""
    return DASHBOARD_HTML


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    """Return storage metrics, config details, and daemon state."""
    stats = pipeline.storage.get_stats()
    return {
        "storage": stats,
        "config": {
            "target_url": config.TARGET_URL,
            "min_points_threshold": config.MIN_POINTS_THRESHOLD,
            "poll_interval_seconds": config.POLL_INTERVAL_SECONDS,
            "max_items_per_run": config.MAX_ITEMS_PER_RUN,
            "dry_run": config.DRY_RUN,
            "webhook_configured": bool(config.DISCORD_WEBHOOK_URL),
        },
        "last_cycle": _last_cycle_stats,
        "is_scraping": _is_scraping,
    }


@app.get("/api/articles")
def get_articles(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent articles tracked in SQLite."""
    return pipeline.storage.get_recent_articles(limit=limit)


@app.get("/api/logs")
def get_logs() -> list[dict[str, Any]]:
    """Return in-memory log buffer for dashboard streaming."""
    return list(recent_logs)


@app.post("/api/scrape")
def manual_scrape() -> dict[str, Any]:
    """Trigger an immediate scraping & notification pass."""
    global _is_scraping, _last_cycle_stats
    if _is_scraping:
        raise HTTPException(status_code=409, detail="A scrape cycle is already in progress.")

    try:
        _is_scraping = True
        metrics = pipeline.run_cycle()
        _last_cycle_stats = {
            "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            **metrics,
        }
        return {"status": "success", "metrics": metrics}
    finally:
        _is_scraping = False


@app.post("/api/test-webhook")
def test_webhook() -> dict[str, Any]:
    """Send a mock test article alert to verify Discord webhook connectivity."""
    test_item = ScrapedItem(
        id=f"test_{int(time.time())}",
        title="Test Alert: High-Performance Python Web Scraper Verified",
        url="https://github.com/sahuom890/news-alert-bot",
        source="Hacker News (Test)",
        score=256,
        comments_count=42,
        author="system_admin",
        published_at="Just now",
    )
    result = pipeline.notifier.send_notification(test_item)
    return {
        "success": result.success,
        "status_code": result.status_code,
        "error": result.error_message,
        "message": "Test alert dispatched successfully!" if result.success else f"Failed: {result.error_message}",
    }


def start_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the Uvicorn web server."""
    logging.getLogger("main").info("Starting Web Dashboard on http://%s:%d", host, port)
    uvicorn.run("app:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    start_server()
