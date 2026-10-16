"""Subscribed iCalendar feeds — fetch, expand, and window an external calendar.

The ``calendar`` custom element renders events the page author typed into its
fence. This module is the other source: a real calendar (Google Calendar, iCloud,
a school's published schedule) that changes without anyone editing the wiki.

Why the server fetches it and not the element
---------------------------------------------
The element's JS runs in the page, so the obvious design is ``fetch()`` straight
from the component. It doesn't work: no calendar host sends
``Access-Control-Allow-Origin``, so the browser refuses to read the response.
Google's ICS endpoints send no CORS headers at all, on both the public and the
secret-URL forms. A same-origin route that fetches server-side is the only way a
browser component can read one.

The URL comes from the page, so the allow-list is the boundary
-------------------------------------------------------------
The `calendar` element carries the calendar's address as a field, so a page can
subscribe to any calendar without touching settings or code. That means
``/api/calendar-feed`` takes the URL from its caller — and a route that fetched
*any* caller-supplied URL would be a server-side request forgery hole, and a wide
one: ``auth.py`` grants loopback callers **owner** rights, and any web page in
the user's browser can fire requests at our loopback port. Such a route would let
a website use this app as a proxy into the user's private network
(``http://192.168.1.1/``, cloud metadata endpoints, other loopback services) —
the response would be blocked by CORS, but the *request* still happens, and for
SSRF the request is the damage.

``validate_url`` is what keeps that shut, and it is the **only** thing that does.
It runs before every fetch and accepts nothing but https at a known calendar
provider. Widening ``ALLOWED_HOSTS`` widens what any web page can ask this app to
reach, so treat it as a security decision rather than a convenience one.

Note what this design gives up: the calendar's address lives in page content, so
anyone who can read the page (or its history, or an export) can read the address,
and a Google "secret address" grants read access to that whole calendar. That is
the trade for a self-contained element — worth saying out loud in the UI, which
is why the element's own help text says it.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

# Hosts we will fetch a calendar from. Deliberately a short list of calendar
# providers rather than "any https URL": the point of the allow-list is that a
# stored setting can't be aimed at the user's router or a cloud metadata service,
# so widening this widens what the app can be pointed at. Subdomains match, so
# "calendar.google.com" covers itself and anything under it.
ALLOWED_HOSTS = (
    "calendar.google.com",
    "calendar.yahoo.com",
    "icloud.com",
    "outlook.office365.com",
    "outlook.live.com",
    "webcal.fm",
)

FETCH_TIMEOUT = 10.0
MAX_BYTES = 8 * 1024 * 1024      # a big family calendar is ~0.5MB; 8 is generous
CACHE_TTL = 300.0                # seconds; a page render must not hammer Google

# How far either side of today we expand recurring events. A calendar with a
# "every Tuesday, forever" rule has unbounded occurrences, so the window is what
# makes expansion terminate at all — not merely a payload optimisation.
DEFAULT_PAST_DAYS = 180
DEFAULT_FUTURE_DAYS = 540

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def local_tzname() -> str:
    """The machine's IANA timezone name, or UTC if it can't be determined.

    The right default for a household wiki: a calendar on the wall shows the day
    an event falls on *here*. An 8pm New York event is stored as the next day in
    UTC and would land on the wrong square if we rendered the calendar's own
    nominal zone (Google sets that to UTC on shared calendars).
    """
    try:
        name = getattr(datetime.now().astimezone().tzinfo, "key", None)
        if name:
            return name
    except Exception:
        pass
    try:                                    # Linux/macOS fallback: /etc/localtime
        import os
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            candidate = link.split("/zoneinfo/", 1)[1]
            ZoneInfo(candidate)             # reject a path that isn't a real zone
            return candidate
    except Exception:
        pass
    return "UTC"


class FeedError(Exception):
    """A feed could not be produced. Message is safe to show a user."""


# --- URL policy ---------------------------------------------------------------

def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def validate_url(url: str) -> str:
    """Return the URL if we're willing to fetch it, else raise ``FeedError``.

    Called on write *and* before every fetch. Checking again at fetch time means
    a settings row edited by some other path (a restored backup, a hand-run
    UPDATE) still can't aim the fetcher somewhere new.
    """
    text = (url or "").strip()
    if not text:
        raise FeedError("no calendar URL")
    # webcal:// is how calendar apps hand these out; it is http(s) underneath.
    if text.lower().startswith("webcal://"):
        text = "https://" + text[len("webcal://"):]
    try:
        parts = urlparse(text)
    except Exception:
        raise FeedError("calendar URL could not be parsed")
    if parts.scheme != "https":
        raise FeedError("calendar URL must be https")
    if not _host_allowed(parts.hostname or ""):
        raise FeedError(
            f"{parts.hostname or 'that host'} is not an allowed calendar host "
            f"({', '.join(ALLOWED_HOSTS)})")
    return text


# --- ICS parsing --------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a leading space/tab continues the line before."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _split_prop(line: str) -> tuple[str, dict, str]:
    """``DTSTART;TZID=America/New_York:20260101T090000`` → name, params, value."""
    head, _, value = line.partition(":")
    bits = head.split(";")
    name = bits[0].strip().upper()
    params = {}
    for b in bits[1:]:
        k, _, v = b.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _zone(tzid: str | None):
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _parse_dt(value: str, params: dict):
    """An ICS date-time → (datetime|date, all_day).

    Three forms appear in the wild and all three appear in Google's output:
    ``20260101`` (VALUE=DATE, all-day), ``20260101T090000Z`` (UTC), and
    ``20260101T090000`` with a TZID param.
    """
    value = (value or "").strip()
    if not value:
        return None, False
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        try:
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8])), True
        except ValueError:
            return None, False
    try:
        naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if value.endswith("Z"):
        return naive.replace(tzinfo=timezone.utc), False
    tz = _zone(params.get("TZID"))
    return (naive.replace(tzinfo=tz) if tz else naive), False


def parse_ics(text: str) -> list[dict]:
    """Pull VEVENTs out of an ICS document as raw dicts.

    Only the fields the calendar element can show, plus what recurrence needs.
    Anything unparseable is skipped rather than fatal — one malformed event in a
    1,400-event calendar should not blank the whole thing.
    """
    events: list[dict] = []
    cur: dict | None = None
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            cur = {"exdates": []}
            continue
        if stripped == "END:VEVENT":
            if cur is not None and cur.get("start") is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        name, params, value = _split_prop(line)
        if name == "SUMMARY":
            cur["title"] = _unescape(value).strip()
        elif name == "UID":
            cur["uid"] = value.strip()
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = _parse_dt(value, params)
        elif name == "DTEND":
            cur["end"], _ = _parse_dt(value, params)
        elif name == "RRULE":
            cur["rrule"] = value.strip()
        elif name == "RECURRENCE-ID":
            cur["recurrence_id"], _ = _parse_dt(value, params)
        elif name == "EXDATE":
            for piece in value.split(","):
                dt, _ = _parse_dt(piece, params)
                if dt is not None:
                    cur["exdates"].append(dt)
        elif name == "STATUS":
            cur["status"] = value.strip().upper()
    return events


# --- recurrence ---------------------------------------------------------------

def _as_datetime(value, tz) -> datetime:
    """Normalise a date-or-datetime to an aware datetime for comparison."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def _expand(event: dict, window_start: datetime, window_end: datetime,
            tz) -> list[datetime | date]:
    """Occurrence start times for one VEVENT inside the window."""
    start = event["start"]
    rule = event.get("rrule")
    if not rule:
        moment = _as_datetime(start, tz)
        return [start] if window_start <= moment <= window_end else []

    from dateutil.rrule import rrulestr        # local: only recurring feeds pay

    all_day = event.get("all_day")
    anchor = _as_datetime(start, tz)
    try:
        rset = rrulestr(rule, dtstart=anchor)
    except Exception:
        return [start] if window_start <= anchor <= window_end else []

    excluded = {_as_datetime(x, tz) for x in event.get("exdates", [])}
    out: list[datetime | date] = []
    try:
        for occurrence in rset.between(window_start, window_end, inc=True):
            if occurrence in excluded:
                continue
            out.append(occurrence.date() if all_day else occurrence)
            if len(out) > 2000:                # a runaway rule is not worth more
                break
    except Exception:
        return []
    return out


def _iso_day(value, tz) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=tz)
        return moment.astimezone(tz).date().isoformat()
    return value.isoformat()


def to_calendar_events(raw: list[dict], *, tz, past_days: int = DEFAULT_PAST_DAYS,
                       future_days: int = DEFAULT_FUTURE_DAYS,
                       now: datetime | None = None) -> list[dict]:
    """Raw VEVENTs → the ``{title,start,end}`` shape the element renders.

    The element is a month grid keyed by day, so times are dropped and each
    occurrence becomes its day (in the calendar's own timezone — an 8pm New York
    event is stored as the next day in UTC, and would land on the wrong square if
    we took the UTC date).
    """
    now = now or datetime.now(tz)
    window_start = now - timedelta(days=past_days)
    window_end = now + timedelta(days=future_days)

    # A RECURRENCE-ID event replaces one occurrence of its UID's rule ("that
    # Tuesday moved to Thursday"). Collect them first so expansion can skip the
    # slot they override, then emit them in their own right.
    overrides: dict[tuple[str, str], dict] = {}
    for event in raw:
        if event.get("recurrence_id") is not None and event.get("uid"):
            overrides[(event["uid"], _iso_day(event["recurrence_id"], tz))] = event

    out: list[dict] = []
    for event in raw:
        if event.get("status") == "CANCELLED":
            continue
        if event.get("recurrence_id") is not None:
            # Emitted below as itself; not expanded as part of the series.
            starts = [event["start"]]
        else:
            starts = _expand(event, window_start, window_end, tz)

        uid = event.get("uid") or ""
        title = event.get("title") or "Untitled"
        # Span in days, so a multi-day event keeps its shape across occurrences.
        span = 0
        if event.get("end") is not None and event.get("start") is not None:
            try:
                span = (_as_datetime(event["end"], tz).date()
                        - _as_datetime(event["start"], tz).date()).days
            except Exception:
                span = 0
            if event.get("all_day") and span > 0:
                span -= 1               # DTEND is exclusive for all-day events
            span = max(0, span)

        for occurrence in starts:
            day = _iso_day(occurrence, tz)
            if event.get("recurrence_id") is None and (uid, day) in overrides:
                continue                # the override supplies this one
            entry = {"title": title, "start": day}
            if span:
                entry["end"] = (date.fromisoformat(day)
                                + timedelta(days=span)).isoformat()
            out.append(entry)

    out.sort(key=lambda e: (e["start"], e["title"]))
    return out


# --- fetch --------------------------------------------------------------------

def fetch_ics(url: str, *, timeout: float = FETCH_TIMEOUT) -> str:
    """GET an allow-listed calendar URL, capped in size."""
    url = validate_url(url)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise FeedError(f"calendar host returned HTTP {resp.status_code}")
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FeedError("calendar feed is too large")
                    chunks.append(chunk)
    except FeedError:
        raise
    except httpx.HTTPError as exc:
        raise FeedError(f"could not reach the calendar host: {exc}") from exc
    return b"".join(chunks).decode("utf-8", "replace")


def events_for_url(url: str, *, tzname: str = "UTC", use_cache: bool = True,
                   **kwargs) -> list[dict]:
    """Fetch + parse + expand one feed URL, memoised for ``CACHE_TTL``."""
    key = f"{url}|{tzname}"
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
                return hit[1]

    tz = _zone(tzname) or timezone.utc
    events = to_calendar_events(parse_ics(fetch_ics(url)), tz=tz, **kwargs)

    if use_cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), events)
    return events


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
