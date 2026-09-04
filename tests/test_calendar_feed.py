"""Subscribed calendar feeds: ICS parsing, recurrence, URL policy, and the route.

Two properties carry the weight here.

**The expansion has to agree with the calendar provider.** A month grid that
quietly drops every third Tuesday is worse than no calendar, and recurrence is
where that happens: ``RRULE`` with ``COUNT``/``UNTIL``/``BYDAY``, ``EXDATE``
holes, and ``RECURRENCE-ID`` rows that move one occurrence. The fixture below is
a reduced copy of the shapes a real Google Calendar emits, and each of those is
pinned separately so a regression names itself.

**The route must not become an open proxy.** ``auth.py`` gives loopback callers
owner rights and any web page can fire requests at our loopback port. The URL
arrives from the page, so ``validate_url`` is the *only* thing standing between
that route and a website using this app to reach the user's router or a cloud
metadata endpoint. The refusal tests below are that boundary — they must never be
relaxed to make something pass.

Nothing here touches the network: ``fetch_ics`` is the seam every test patches.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from waikiki import calendarfeed as cf
from waikiki import db, store
from waikiki.api import app

NY = ZoneInfo("America/New_York")

# A reduced Google Calendar: one plain event, one all-day, one weekly series with
# a hole punched in it, an override that moves a single occurrence, a bounded
# series, a folded line, an escaped summary, and a cancelled row.
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:plain@test
DTSTART;TZID=America/New_York:20260910T090000
DTEND;TZID=America/New_York:20260910T100000
SUMMARY:Chess Club
END:VEVENT
BEGIN:VEVENT
UID:allday@test
DTSTART;VALUE=DATE:20260907
DTEND;VALUE=DATE:20260908
SUMMARY:No School
END:VEVENT
BEGIN:VEVENT
UID:multi@test
DTSTART;VALUE=DATE:20260911
DTEND;VALUE=DATE:20260914
SUMMARY:Campout
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
DTSTART;TZID=America/New_York:20260901T193000
DTEND;TZID=America/New_York:20260901T203000
RRULE:FREQ=WEEKLY;BYDAY=TU
EXDATE;TZID=America/New_York:20260915T193000
SUMMARY:Scout Meeting
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
RECURRENCE-ID;TZID=America/New_York:20260908T193000
DTSTART;TZID=America/New_York:20260910T193000
DTEND;TZID=America/New_York:20260910T203000
SUMMARY:Scout Meeting (moved)
END:VEVENT
BEGIN:VEVENT
UID:bounded@test
DTSTART;TZID=America/New_York:20260902T160000
DTEND;TZID=America/New_York:20260902T170000
RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE
SUMMARY:Cello Lesson
END:VEVENT
BEGIN:VEVENT
UID:folded@test
DTSTART;VALUE=DATE:20260918
SUMMARY:Back to School Night at the
  high school
END:VEVENT
BEGIN:VEVENT
UID:escaped@test
DTSTART;VALUE=DATE:20260919
SUMMARY:Pizza\\, popcorn\\, and a movie
END:VEVENT
BEGIN:VEVENT
UID:cancelled@test
DTSTART;VALUE=DATE:20260920
SUMMARY:Called Off
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=NY)


def expand(**kw):
    return cf.to_calendar_events(cf.parse_ics(ICS), tz=NY, now=NOW, **kw)


def days_for(title, events):
    return sorted(e["start"] for e in events if e["title"] == title)


ICS_URL = "https://calendar.google.com/calendar/ical/x/public/basic.ics"


@pytest.fixture
def stub_fetch(monkeypatch):
    """Serve the fixture ICS instead of reaching a calendar host."""
    monkeypatch.setattr(cf, "fetch_ics", lambda url, **kw: ICS)
    cf.clear_cache()
    yield
    cf.clear_cache()


# --- parsing ------------------------------------------------------------------

def test_parses_every_event_including_folded_and_escaped():
    raw = cf.parse_ics(ICS)
    assert len(raw) == 9
    titles = {e.get("title") for e in raw}
    # RFC 5545 line folding: the continuation belongs to the summary before it.
    assert "Back to School Night at the high school" in titles
    # A comma inside a SUMMARY is escaped on the wire and must come back plain.
    assert "Pizza, popcorn, and a movie" in titles


def test_malformed_event_is_skipped_not_fatal():
    broken = ICS.replace("DTSTART;VALUE=DATE:20260907", "DTSTART;VALUE=DATE:notadate")
    raw = cf.parse_ics(broken)
    assert len(raw) == 8                       # the bad one dropped
    assert cf.to_calendar_events(raw, tz=NY, now=NOW)   # the rest still render


def test_cancelled_events_are_not_shown():
    assert not days_for("Called Off", expand())


# --- recurrence ---------------------------------------------------------------

def test_weekly_series_expands_across_the_window():
    days = days_for("Scout Meeting", expand())
    assert "2026-09-01" in days and "2026-09-22" in days and "2026-09-29" in days
    assert len(days) > 20                       # keeps going, not just a few


def test_exdate_removes_that_one_occurrence():
    days = days_for("Scout Meeting", expand())
    assert "2026-09-15" not in days             # EXDATE punched this hole
    assert "2026-09-22" in days                 # neighbours untouched


def test_recurrence_id_moves_a_single_occurrence():
    events = expand()
    # The Sept 8 slot is surrendered to the override...
    assert "2026-09-08" not in days_for("Scout Meeting", events)
    # ...which appears on its new day, under its own title.
    assert days_for("Scout Meeting (moved)", events) == ["2026-09-10"]


def test_count_bounds_a_series():
    assert days_for("Cello Lesson", expand()) == [
        "2026-09-02", "2026-09-09", "2026-09-16"]


def test_multi_day_event_keeps_its_span():
    campout = [e for e in expand() if e["title"] == "Campout"]
    assert len(campout) == 1
    # DTEND is exclusive for all-day events: 11th–14th on the wire is 11th–13th.
    assert campout[0] == {"title": "Campout", "start": "2026-09-11",
                          "end": "2026-09-13"}


def test_window_bounds_an_unbounded_rule():
    """A 'forever' rule terminates because of the window, not by luck."""
    narrow = expand(past_days=3, future_days=10)
    days = days_for("Scout Meeting", narrow)
    assert days and all("2026-09-01" <= d <= "2026-09-14" for d in days)


# --- timezone -----------------------------------------------------------------

def test_event_lands_on_its_local_day_not_its_utc_day():
    """A late-evening event must not slide to tomorrow.

    ``20260924T000000Z`` is 8pm on the 23rd in New York. Rendering the UTC date
    would put a Wednesday-evening event on Thursday's square — the bug this
    pins. (A real event on the family calendar has exactly this shape.)
    """
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:late@test\n"
           "DTSTART:20260924T000000Z\nDTEND:20260924T010000Z\n"
           "SUMMARY:Virtual Meeting\nEND:VEVENT\nEND:VCALENDAR\n")
    raw = cf.parse_ics(ics)
    assert cf.to_calendar_events(raw, tz=NY, now=NOW)[0]["start"] == "2026-09-23"
    # Same instant, rendered for a UTC household, is the following day.
    assert cf.to_calendar_events(
        raw, tz=ZoneInfo("UTC"),
        now=datetime(2026, 9, 4, tzinfo=ZoneInfo("UTC")))[0]["start"] == "2026-09-24"


def test_local_tzname_is_a_real_zone():
    ZoneInfo(cf.local_tzname())                 # raises if we invented a name


# --- URL policy: the whole boundary ---------------------------------------------

@pytest.mark.parametrize("url", [
    "https://calendar.google.com/calendar/ical/x/public/basic.ics",
    "webcal://calendar.google.com/calendar/ical/x/public/basic.ics",
    "https://p12-caldav.icloud.com/published/2/abc",
])
def test_allowed_calendar_urls(url):
    assert cf.validate_url(url).startswith("https://")


@pytest.mark.parametrize("url", [
    "http://calendar.google.com/calendar/ical/x/basic.ics",   # not https
    "https://192.168.1.1/admin",                              # private network
    "https://169.254.169.254/latest/meta-data/",              # cloud metadata
    "http://localhost:8787/settings",                         # ourselves
    "https://evil.example.com/x.ics",                         # not a calendar host
    "https://calendar.google.com.evil.com/x.ics",             # suffix look-alike
    "file:///etc/passwd",
    "",
])
def test_refused_calendar_urls(url):
    """The page supplies this URL, so this list is the security boundary."""
    with pytest.raises(cf.FeedError):
        cf.validate_url(url)


def test_refusal_happens_before_any_connection(monkeypatch):
    opened = []
    monkeypatch.setattr(cf.httpx, "Client", lambda *a, **k: opened.append(1))
    with pytest.raises(cf.FeedError):
        cf.fetch_ics("https://169.254.169.254/latest/")
    assert not opened


# --- the route ------------------------------------------------------------------

def client():
    return TestClient(app, client=("127.0.0.1", 12345))


def test_route_serves_the_calendar_named_by_the_page(wiki, stub_fetch):
    with client() as c:
        r = c.get("/api/calendar-feed", params={"url": ICS_URL, "tz": "America/New_York"})
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "America/New_York"
    titles = {e["title"] for e in body["events"]}
    assert "Chess Club" in titles and "Scout Meeting" in titles


def test_route_defaults_to_the_local_timezone(wiki, stub_fetch):
    with client() as c:
        r = c.get("/api/calendar-feed", params={"url": ICS_URL})
    assert r.json()["timezone"] == cf.local_tzname()


@pytest.mark.parametrize("url", [
    "https://192.168.1.1/admin",
    "https://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8787/settings",
    "https://evil.example.com/x.ics",
    "file:///etc/passwd",
])
def test_route_refuses_to_be_an_open_proxy(wiki, url, monkeypatch):
    """The reason this route can be handed a URL at all."""
    opened = []
    monkeypatch.setattr(cf.httpx, "Client", lambda *a, **k: opened.append(1))
    with client() as c:
        r = c.get("/api/calendar-feed", params={"url": url})
    assert r.status_code == 502
    assert not opened, "refused before opening a connection"


def test_route_requires_a_url(wiki):
    with client() as c:
        assert c.get("/api/calendar-feed").status_code == 422


def test_upstream_failure_is_reported_not_silently_empty(wiki, monkeypatch):
    """An empty month would read as 'nothing scheduled'. Say it broke instead."""
    def boom(url, **kw):
        raise cf.FeedError("calendar host returned HTTP 404")
    monkeypatch.setattr(cf, "fetch_ics", boom)
    cf.clear_cache()
    with client() as c:
        r = c.get("/api/calendar-feed", params={"url": ICS_URL})
    assert r.status_code == 502
    assert "404" in r.json()["detail"]


def test_oversized_feed_is_refused(monkeypatch):
    class FakeResp:
        status_code = 200
        def iter_bytes(self):
            yield b"x" * (cf.MAX_BYTES + 1)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, *a, **k): return FakeResp()

    monkeypatch.setattr(cf.httpx, "Client", FakeClient)
    with pytest.raises(cf.FeedError, match="too large"):
        cf.fetch_ics(ICS_URL)


def test_results_are_cached_so_a_render_does_not_hammer_the_host(monkeypatch):
    calls = []
    monkeypatch.setattr(cf, "fetch_ics", lambda url, **kw: calls.append(url) or ICS)
    cf.clear_cache()
    cf.events_for_url(ICS_URL, tzname="America/New_York")
    cf.events_for_url(ICS_URL, tzname="America/New_York")
    assert len(calls) == 1
    cf.clear_cache()
    cf.events_for_url(ICS_URL, tzname="America/New_York")
    assert len(calls) == 2
