"""Economic-calendar news filter.

Scalping through a high-impact release (NFP, CPI, FOMC...) is how tight
stops get skipped by several multiples. This module:

  * downloads the free Forex Factory weekly calendar (JSON, no API key),
  * caches it on disk and refreshes on a TTL, so a flaky connection or an
    offline stretch doesn't break the bot,
  * blocks NEW entries in a window around events that affect a symbol's
    currencies, and
  * optionally asks the bot to FLATTEN open positions shortly before
    high-impact events.

If the feed is unreachable the bot keeps running: by default it trades
without news protection and logs a prominent warning (set fail_closed: true
to halt entries instead until the calendar is available again).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .config import NewsConfig

log = logging.getLogger(__name__)

FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]


@dataclass
class NewsEvent:
    time: datetime          # tz-aware UTC
    currency: str           # e.g. "USD"
    title: str              # e.g. "Non-Farm Employment Change"
    impact: str             # "High" | "Medium" | "Low" | "Holiday"


def _http_fetch(url: str) -> list:
    import requests

    resp = requests.get(url, timeout=15, headers={"User-Agent": "mt5-scalper-bot"})
    resp.raise_for_status()
    return resp.json()


class NewsCalendar:
    """Fetches, caches, and parses the calendar feed."""

    def __init__(
        self, cfg: NewsConfig, cache_dir: str | Path,
        fetcher: Callable[[str], list] | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache_path = Path(cache_dir) / "news_calendar.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._fetch = fetcher or _http_fetch
        self._events: List[NewsEvent] = []
        self._fetched_at: Optional[datetime] = None
        self._last_attempt: Optional[datetime] = None
        self._load_cache()

    # ---- fetching / caching ---------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            self._events = self._parse(data["events"])
            self._fetched_at = datetime.fromisoformat(data["fetched_at"])
            log.info("News: loaded %d cached events", len(self._events))
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("News: cache unreadable, will re-fetch")

    def maybe_refresh(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        fresh = self._fetched_at and \
            now - self._fetched_at < timedelta(hours=self.cfg.refresh_hours)
        if fresh:
            return
        # after a failure, don't hammer the feed — retry every 15 minutes
        if self._last_attempt and now - self._last_attempt < timedelta(minutes=15):
            return
        self._last_attempt = now
        try:
            raw: list = []
            for url in FEED_URLS:
                raw.extend(self._fetch(url))
        except Exception as exc:
            log.warning(
                "News: calendar fetch failed (%s) — %s",
                exc,
                "ENTRIES HALTED until feed returns (fail_closed)"
                if self.cfg.fail_closed and not self._events
                else f"continuing with {len(self._events)} known events",
            )
            return
        self._events = self._parse(raw)
        self._fetched_at = now
        self.cache_path.write_text(json.dumps({
            "fetched_at": now.isoformat(),
            "events": raw,
        }))
        log.info("News: calendar refreshed, %d events this/next week", len(self._events))

    @staticmethod
    def _parse(raw: list) -> List[NewsEvent]:
        events = []
        for item in raw:
            try:
                when = datetime.fromisoformat(item["date"])
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                events.append(NewsEvent(
                    time=when.astimezone(timezone.utc),
                    currency=str(item.get("country", "")).upper(),
                    title=item.get("title", "?"),
                    impact=str(item.get("impact", "")).capitalize(),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(events, key=lambda e: e.time)

    def events(self) -> List[NewsEvent]:
        return self._events

    def has_data(self, now: Optional[datetime] = None) -> bool:
        """True if the calendar plausibly covers 'now'."""
        if not self._events:
            return False
        now = now or datetime.now(timezone.utc)
        return self._events[0].time - timedelta(days=7) <= now \
            <= self._events[-1].time + timedelta(days=3)


class NewsFilter:
    """Applies the calendar to symbols."""

    def __init__(self, cfg: NewsConfig, calendar: NewsCalendar) -> None:
        self.cfg = cfg
        self.calendar = calendar
        self._impacts = {i.capitalize() for i in cfg.impacts}

    def currencies_for(self, symbol: str) -> List[str]:
        base = symbol.upper().split(".")[0].split("_")[0]
        override = self.cfg.currency_map.get(base) or self.cfg.currency_map.get(symbol)
        if override:
            return [c.upper() for c in override]
        if len(base) >= 6 and base[:6].isalpha():
            return [base[:3], base[3:6]]
        log.debug("News: don't know which currencies drive %s", symbol)
        return []

    def _relevant(self, symbol: str, now: datetime, before_min: float,
                  after_min: float) -> Optional[NewsEvent]:
        currencies = set(self.currencies_for(symbol))
        if not currencies:
            return None
        for event in self.calendar.events():
            if event.impact not in self._impacts or event.currency not in currencies:
                continue
            start = event.time - timedelta(minutes=before_min)
            end = event.time + timedelta(minutes=after_min)
            if start <= now <= end:
                return event
        return None

    def check_entry(self, symbol: str, now: Optional[datetime] = None) -> tuple[bool, str]:
        """(allowed, reason). Blocks entries near relevant events."""
        now = now or datetime.now(timezone.utc)
        if not self.calendar.has_data(now):
            if self.cfg.fail_closed:
                return False, "news calendar unavailable (fail_closed=true)"
            return True, ""
        event = self._relevant(
            symbol, now, self.cfg.block_minutes_before, self.cfg.block_minutes_after
        )
        if event is not None:
            delta = (event.time - now).total_seconds() / 60
            when = f"in {delta:.0f} min" if delta >= 0 else f"{-delta:.0f} min ago"
            return False, (
                f"news: {event.currency} {event.title} ({event.impact}) {when}"
            )
        return True, ""

    def should_flatten(self, symbol: str, now: Optional[datetime] = None
                       ) -> Optional[NewsEvent]:
        """Event that warrants closing open positions now (pre-event only)."""
        if not self.cfg.flatten_before_news:
            return None
        now = now or datetime.now(timezone.utc)
        event = self._relevant(symbol, now, self.cfg.flatten_minutes_before, 0)
        # only flatten BEFORE the event, not in the post-event window
        if event is not None and event.time >= now:
            return event
        return None
