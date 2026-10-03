"""Unit tests for the Hacker News web scraper module."""

import pytest
from scraper import HackerNewsScraper, ScrapedItem

SAMPLE_HN_HTML = """
<html op="news">
<head><title>Hacker News</title></head>
<body>
<table border="0" cellpadding="0" cellspacing="0" class="itemlist">
  <tr class="athing" id="39485721">
    <td align="right" valign="top" class="title"><span class="rank">1.</span></td>
    <td valign="top" class="votelinks"></td>
    <td class="title">
      <span class="titleline">
        <a href="https://example.com/fast-database">Building a High-Performance Database from Scratch</a>
        <span class="sitebit comhead"> (<a href="from?site=example.com"><span class="sitestr">example.com</span></a>)</span>
      </span>
    </td>
  </tr>
  <tr>
    <td colspan="2"></td>
    <td class="subtext">
      <span class="subline">
        <span class="score" id="score_39485721">185 points</span> by 
        <a href="user?id=alexdev" class="hnuser">alexdev</a> 
        <span class="age" title="2026-10-03T10:00:00"><a href="item?id=39485721">3 hours ago</a></span> | 
        <a href="hide?id=39485721">hide</a> | 
        <a href="item?id=39485721">42&nbsp;comments</a>
      </span>
    </td>
  </tr>
  <tr class="spacer" style="height:5px"></tr>
  <tr class="athing" id="39485722">
    <td align="right" valign="top" class="title"><span class="rank">2.</span></td>
    <td valign="top" class="votelinks"></td>
    <td class="title">
      <span class="titleline">
        <a href="item?id=39485722">Ask HN: Best practices for Python Daemons?</a>
      </span>
    </td>
  </tr>
  <tr>
    <td colspan="2"></td>
    <td class="subtext">
      <span class="subline">
        <span class="score" id="score_39485722">35 points</span> by 
        <a href="user?id=pyguru" class="hnuser">pyguru</a> 
        <span class="age" title="2026-10-03T11:00:00"><a href="item?id=39485722">2 hours ago</a></span> | 
        <a href="hide?id=39485722">hide</a> | 
        <a href="item?id=39485722">18&nbsp;comments</a>
      </span>
    </td>
  </tr>
  <tr class="spacer" style="height:5px"></tr>
  <tr class="athing" id="39485723">
    <td align="right" valign="top" class="title"><span class="rank">3.</span></td>
    <td class="title">
      <span class="titleline">
        <a href="https://jobs.example.com/python-eng">Stripe is hiring senior backend engineers</a>
      </span>
    </td>
  </tr>
  <tr>
    <td colspan="2"></td>
    <td class="subtext">
      <span class="age"><a href="item?id=39485723">1 hour ago</a></span>
    </td>
  </tr>
</table>
</body>
</html>
"""


def test_hn_scraper_parse_items() -> None:
    """Test parsing HTML into structured ScrapedItem instances."""
    scraper = HackerNewsScraper()
    items = scraper.parse(SAMPLE_HN_HTML, max_items=10)

    assert len(items) == 3

    # First item verification
    first = items[0]
    assert first.id == "hn_39485721"
    assert first.title == "Building a High-Performance Database from Scratch"
    assert first.url == "https://example.com/fast-database"
    assert first.score == 185
    assert first.author == "alexdev"
    assert first.comments_count == 42
    assert first.published_at == "3 hours ago"

    # Second item (Relative URL resolution for Ask HN)
    second = items[1]
    assert second.id == "hn_39485722"
    assert second.title == "Ask HN: Best practices for Python Daemons?"
    assert second.url == "https://news.ycombinator.com/item?id=39485722"
    assert second.score == 35
    assert second.comments_count == 18

    # Third item (Job posting with no score/comments)
    third = items[2]
    assert third.id == "hn_39485723"
    assert third.title == "Stripe is hiring senior backend engineers"
    assert third.score == 0
    assert third.comments_count == 0


def test_hn_scraper_max_items_limit() -> None:
    """Test respecting the max_items parameter."""
    scraper = HackerNewsScraper()
    items = scraper.parse(SAMPLE_HN_HTML, max_items=1)
    assert len(items) == 1
    assert items[0].id == "hn_39485721"


def test_hn_scraper_malformed_html() -> None:
    """Ensure scraper handles empty or broken HTML gracefully without crashing."""
    scraper = HackerNewsScraper()
    assert scraper.parse("", max_items=10) == []
    assert scraper.parse("<div>Broken markup with no table</div>", max_items=10) == []
    assert scraper.parse("<tr class='athing'></tr>", max_items=10) == []
