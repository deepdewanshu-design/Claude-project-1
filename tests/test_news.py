from datetime import datetime, timedelta, timezone

import pytest

from scalper.config import NewsConfig
from scalper.news import NewsCalendar, NewsFilter

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)  # a Monday


def feed(events):
    """Build a fake Forex Factory feed (one URL's worth; second URL empty)."""
    calls = {"n": 0}

    def fetcher(url):
        calls["n"] += 1
        return events if calls["n"] % 2 == 1 else []

    return fetcher


def make_filter(tmp_path, events, **cfg_kwargs):
    cfg = NewsConfig(**cfg_kwargs)
    cal = NewsCalendar(cfg, tmp_path / "cache", fetcher=feed(events))
    cal.maybe_refresh(NOW)
    return NewsFilter(cfg, cal), cal


def ff_event(minutes_from_now, currency="USD", impact="High", title="CPI y/y"):
    when = NOW + timedelta(minutes=minutes_from_now)
    return {"title": title, "country": currency,
            "date": when.isoformat(), "impact": impact}


def test_blocks_entry_before_and_after_event(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10)])
    ok, why = filt.check_entry("EURUSD", NOW)  # USD event, EURUSD affected
    assert not ok and "CPI" in why
    # 10 min after the event is inside the 15-min post window
    assert not filt.check_entry("EURUSD", NOW + timedelta(minutes=20))[0]
    # 40 min after is clear
    assert filt.check_entry("EURUSD", NOW + timedelta(minutes=50))[0]


def test_unrelated_currency_not_blocked(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10, currency="JPY")])
    assert filt.check_entry("EURUSD", NOW)[0]


def test_low_impact_ignored_by_default(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10, impact="Low")])
    assert filt.check_entry("EURUSD", NOW)[0]


def test_medium_impact_blocked_when_configured(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10, impact="Medium")],
                          impacts=["High", "Medium"])
    assert not filt.check_entry("EURUSD", NOW)[0]


def test_gold_blocked_by_usd_news_via_currency_map(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10)])
    assert not filt.check_entry("XAUUSD", NOW)[0]
    assert not filt.check_entry("US30", NOW)[0]


def test_broker_suffix_stripped(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(10, currency="GBP")])
    assert not filt.check_entry("GBPUSD.x", NOW)[0]


def test_flatten_only_shortly_before_event(tmp_path):
    filt, _ = make_filter(tmp_path, [ff_event(30)])
    assert filt.should_flatten("EURUSD", NOW) is None            # 30 min out: hold
    assert filt.should_flatten(
        "EURUSD", NOW + timedelta(minutes=26)) is not None       # 4 min out: flatten
    assert filt.should_flatten(
        "EURUSD", NOW + timedelta(minutes=31)) is None           # after: don't churn


def test_fail_open_when_feed_down(tmp_path):
    def broken(url):
        raise ConnectionError("proxy says no")

    cfg = NewsConfig()
    cal = NewsCalendar(cfg, tmp_path / "cache", fetcher=broken)
    cal.maybe_refresh(NOW)
    filt = NewsFilter(cfg, cal)
    ok, _ = filt.check_entry("EURUSD", NOW)
    assert ok  # fail-open default: keep trading, no data


def test_fail_closed_blocks_without_data(tmp_path):
    def broken(url):
        raise ConnectionError("proxy says no")

    cfg = NewsConfig(fail_closed=True)
    cal = NewsCalendar(cfg, tmp_path / "cache", fetcher=broken)
    cal.maybe_refresh(NOW)
    filt = NewsFilter(cfg, cal)
    ok, why = filt.check_entry("EURUSD", NOW)
    assert not ok and "unavailable" in why


def test_cache_survives_restart(tmp_path):
    _, cal = make_filter(tmp_path, [ff_event(10)])
    assert cal.cache_path.exists()
    # new calendar instance with a dead fetcher must load events from cache
    def broken(url):
        raise ConnectionError("offline")

    cal2 = NewsCalendar(NewsConfig(), tmp_path / "cache", fetcher=broken)
    assert len(cal2.events()) == 1
    assert cal2.has_data(NOW)


def test_refresh_respects_ttl(tmp_path):
    events = [ff_event(10)]
    calls = {"n": 0}

    def counting(url):
        calls["n"] += 1
        return events

    cal = NewsCalendar(NewsConfig(refresh_hours=6), tmp_path / "cache",
                       fetcher=counting)
    cal.maybe_refresh(NOW)
    first = calls["n"]
    cal.maybe_refresh(NOW + timedelta(hours=1))   # inside TTL — no fetch
    assert calls["n"] == first
    cal.maybe_refresh(NOW + timedelta(hours=7))   # TTL expired — fetch again
    assert calls["n"] > first
