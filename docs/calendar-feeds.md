# Calendar feeds

A `calendar` custom element can render events typed into the page. It can also
**subscribe** to a real calendar — Google Calendar, iCloud, a school's published
schedule — so the wiki shows what is actually happening without anyone editing it.

````
```calendar
ics: https://calendar.google.com/calendar/ical/.../basic.ics
tz: America/New_York
events: [{"title":"Troop 63 Campout","start":"2026-09-11","end":"2026-09-13","link":"troop-63-campout"}]
```
````

Both sources render together. `ics` supplies the live schedule; the `events` list
keeps the entries that link to pages in this wiki, which a feed can't do. `tz` is
optional and defaults to the machine's timezone.

**Adding one:** edit the page, and put the calendar's iCal (`.ics`) address on an
`ics:` line inside the `calendar` block. In Google Calendar that address is
*Settings → your calendar → Integrate calendar → Secret address in iCal format*;
iCloud and Outlook publish an equivalent. Nothing else to configure.

## Why the server fetches it

The obvious design is `fetch()` from inside the component, and it does not work.
No calendar provider sends `Access-Control-Allow-Origin`, so the browser refuses
to read the response. Google's ICS endpoints send no CORS headers at all — on
both the public form and the "secret address" form. A same-origin route that
fetches server-side is the only way a browser component can read one.

That route is `GET /api/calendar-feed?url=...&tz=...`.

## The allow-list is the whole boundary

The URL arrives from the page, so this route will fetch a URL its caller chose —
and that is exactly what makes it dangerous if left unguarded. `auth.py` grants
**loopback callers owner rights**, and any web page open in the user's browser
can fire requests at our loopback port. Unguarded, it would let an arbitrary
website use Waikiki as a proxy into the user's private network —
`http://192.168.1.1/`, cloud metadata endpoints, other services bound to
localhost. CORS would stop that site from *reading* the reply, but the request
still happens, and for SSRF the request is the damage.

`calendarfeed.validate_url` is what keeps that shut, and it is the only thing
that does:

- Only `https` is accepted (`webcal://` is rewritten to it).
- The host must be in `ALLOWED_HOSTS` — a short list of calendar providers.
- Matching is exact-or-subdomain, so `calendar.google.com.evil.com` is refused
  rather than passing a naive suffix check.
- It runs before every fetch, so nothing reaches the network unchecked.

Widening `ALLOWED_HOSTS` widens what any web page can ask this app to reach.
Treat it as a security decision, not a convenience one.

## What this design gives up

The calendar's address lives in page content. Anyone who can read the page — or
its history, or an export, or a backup — can read the address, and a Google
"secret address" grants read access to that entire calendar. That is the trade
for an element a page can point anywhere without touching settings or code. For a
local wiki it is usually the right trade; for a shared one, prefer a calendar
whose contents you would not mind a reader seeing.

## Recurrence

Real calendars are mostly recurring events, and this is where a naive
implementation quietly loses days. The expansion handles:

- `RRULE` — `FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (including `2SU`-style
  nth-weekday), `BYMONTH`. Expanded with `dateutil.rrule`, not by hand.
- `EXDATE` — occurrences deleted from a series.
- `RECURRENCE-ID` — a single occurrence moved or retitled. The generated
  occurrence it replaces is suppressed so the event does not appear twice.
- `STATUS:CANCELLED` — dropped.
- Multi-day events, remembering that all-day `DTEND` is **exclusive**.

A rule like "every Tuesday, forever" has unbounded occurrences, so expansion is
bounded by a window (`DEFAULT_PAST_DAYS` / `DEFAULT_FUTURE_DAYS` around today).
The window is what makes it terminate, not merely a payload optimisation.

### Timezones

Events are placed on the day they fall on **here**, in the feed's configured
timezone (defaulting to the machine's). This matters more than it sounds: an 8pm
New York event is stored as `T000000Z` the *next day*, so rendering the UTC date
would put a Wednesday-evening event on Thursday's square. Google also sets
`X-WR-TIMEZONE: UTC` on shared calendars regardless of where the household is,
so the calendar's own nominal zone is not a useful default.

## Failure is reported, not swallowed

If the calendar host is unreachable or returns an error, the route answers
**502** and the element shows the reason. An empty month grid would read as
"nothing is scheduled", which is a different and worse claim than "the calendar
didn't load".

Responses are cached for `CACHE_TTL` (5 minutes) so a page render doesn't hammer
the provider, and capped at `MAX_BYTES`.

`tests/test_calendar_feed.py` guards the parsing, the recurrence rules, the URL
policy, and the refusals that keep the route from becoming an open proxy.
