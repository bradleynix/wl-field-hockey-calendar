#!/usr/bin/env python3
"""Scrape the Washington-Liberty varsity field hockey schedule and build an ICS feed.

The source site injects its schedule dynamically, so this script uses Playwright/Chromium.
It intentionally uses DOM/text heuristics rather than brittle CSS class names so minor site
redesigns are less likely to break the feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SOURCE_URL = os.getenv(
    "SOURCE_URL",
    "https://www.wlgeneralsathletics.com/fall-sports/field-hockey/",
)
CALENDAR_NAME = os.getenv("CALENDAR_NAME", "W-L Varsity Field Hockey")
TEAM_SHORT = os.getenv("TEAM_SHORT", "W-L Field Hockey")
TEAM_TOKENS = [
    t.strip().lower()
    for t in os.getenv(
        "TEAM_TOKENS",
        "Washington-Liberty|Washington Liberty|Washington-Liberty HS|W-L",
    ).split("|")
    if t.strip()
]
LOCAL_TZ_NAME = os.getenv("TIMEZONE", "America/New_York")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
SEASON_YEAR = int(os.getenv("SEASON_YEAR", str(datetime.now(LOCAL_TZ).year)))
DEFAULT_DURATION_MINUTES = int(os.getenv("DEFAULT_DURATION_MINUTES", "120"))
MIN_EVENTS = int(os.getenv("MIN_EVENTS", "3"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "site"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))

DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?"
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:uary|ruary|ch|il|e|y|ust|tember|ober|ember)?"
DATE_RE = re.compile(
    rf"\b(?P<dow>{DAY}),?\s+(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:,?\s+(?P<year>20\d{{2}}))?\b",
    re.I,
)
TIME_RE = re.compile(r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\b", re.I)

GENERIC_LINES = {
    "varsity",
    "schedule",
    "schedules",
    "field hockey",
    "field hockey girls varsity",
    "girls varsity",
    "details",
    "view details",
    "location",
    "opponent",
    "date/time",
    "results",
    "type",
    "links",
    "vs",
    "@",
    "-",
}

VENUE_HINTS = (
    "high school",
    " hs",
    "stadium",
    "field",
    "turf",
    "school",
    "complex",
    "park",
    "academy",
    "center",
    "centre",
)


@dataclass
class Event:
    start_local: datetime
    end_local: datetime
    opponent: str
    location: str
    relation: str  # "vs", "@", or "vs" fallback
    details: str
    source_event_url: str
    source_order: int
    uid: str = ""


def clean_text(value: str) -> str:
    value = value.replace("\xa0", " ").replace("\u200b", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    return value.strip()


def lines_of(text: str) -> list[str]:
    # Add a separator for occasionally concatenated date/time text such as "Tue, Aug 117:30 PM".
    text = re.sub(
        rf"({DAY},?\s+{MONTH}\s+\d{{1,2}})(?=\d{{1,2}}:\d{{2}}\s*(?:AM|PM))",
        r"\1\n",
        text,
        flags=re.I,
    )
    out: list[str] = []
    for raw in text.splitlines():
        line = clean_text(raw)
        if line:
            out.append(line)
    return out


def is_own_team(line: str) -> bool:
    low = line.lower()
    return any(tok in low for tok in TEAM_TOKENS)


def is_generic(line: str) -> bool:
    low = line.strip().lower()
    if low in GENERIC_LINES:
        return True
    if "logo" in low:
        return True
    if re.fullmatch(r"[-–—\s]*", line):
        return True
    if re.fullmatch(r"\d{1,3}(?:\s*[-–]\s*\d{1,3})?", line):
        return True
    return False


def parse_local_datetime(text: str) -> datetime | None:
    dm = DATE_RE.search(text)
    tm = TIME_RE.search(text)
    if not dm or not tm:
        return None

    month_name = dm.group("month")[:3].title()
    month_num = datetime.strptime(month_name, "%b").month
    year = int(dm.group("year") or SEASON_YEAR)
    day = int(dm.group("day"))
    hour = int(tm.group("hour"))
    minute = int(tm.group("minute"))
    ampm = tm.group("ampm").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month_num, day, hour, minute, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def choose_source_url(hrefs: Iterable[str]) -> str:
    # Only trust links that look like a unique event/game details URL. Team/opponent links
    # are often repeated across multiple games and would create duplicate ICS UIDs.
    for href in hrefs:
        low = href.lower()
        if href == SOURCE_URL:
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if "assets-" in low or "cdn." in low:
            continue
        if re.search(r"(?:event|game|contest|match)[^/?:#]*[/=?&-]?\d+", low) or re.search(
            r"[?&](?:event|game|contest|match)(?:id)?=", low
        ):
            return href
    return SOURCE_URL


def infer_opponent(block_lines: list[str], full_text: str) -> str:
    # Prefer school-looking lines that are not W-L itself.
    candidates: list[str] = []
    for line in block_lines:
        if DATE_RE.search(line) or TIME_RE.search(line) or is_generic(line) or is_own_team(line):
            continue
        candidates.append(line)

    schoolish = [
        c for c in candidates
        if any(hint in f" {c.lower()}" for hint in VENUE_HINTS)
    ]
    if schoolish:
        # Opponent usually appears before the venue in an event card.
        return schoolish[0]

    # Otherwise take the first meaningful label after the time line.
    time_seen = False
    for line in block_lines:
        if TIME_RE.search(line):
            time_seen = True
            continue
        if time_seen and not (DATE_RE.search(line) or is_generic(line) or is_own_team(line)):
            return line

    return "Opponent TBD"


def infer_relation(text: str, location: str, opponent: str) -> str:
    # Explicit markers are best.
    if re.search(r"(^|\n|\s)@(\s|\n|$)", text):
        return "@"
    if re.search(r"(^|\n|\s)vs\.?($|\s|\n)", text, re.I):
        return "vs"

    # If the inferred venue names W-L, it is probably a home game.
    if location and is_own_team(location):
        return "vs"
    # If the venue repeats the opponent, it is probably away.
    if location and opponent.lower() in location.lower():
        return "@"
    return "vs"


def infer_location(block_lines: list[str], opponent: str) -> str:
    venue_candidates: list[str] = []
    for line in block_lines:
        if DATE_RE.search(line) or TIME_RE.search(line) or is_generic(line):
            continue
        low = f" {line.lower()}"
        if any(hint in low for hint in VENUE_HINTS):
            venue_candidates.append(line)

    # Cards commonly list opponent first and venue last, so use the final venue-like line.
    # If the last label is only "Stadium"/"Field"/"Turf", retain the school name too.
    if venue_candidates:
        last = venue_candidates[-1]
        if last.strip().lower() in {"stadium", "field", "turf"} and len(venue_candidates) >= 2:
            return f"{venue_candidates[-2]} - {last}"
        return last
    return ""


def parse_blocks(blocks: list[dict]) -> list[Event]:
    events: list[Event] = []
    seen_mutable: set[tuple] = set()

    for order, block in enumerate(blocks):
        raw_text = clean_text(block.get("text", ""))
        if not raw_text:
            continue
        start = parse_local_datetime(raw_text)
        if not start:
            continue

        # Field hockey season is a fall sport; this also excludes June/July Green Day text
        # if a broader container slips into the heuristic extraction.
        if start.year != SEASON_YEAR or start.month < 8 or start.month > 11:
            continue

        block_lines = lines_of(raw_text)
        opponent = infer_opponent(block_lines, raw_text)
        location = infer_location(block_lines, opponent)
        relation = infer_relation(raw_text, location, opponent)
        source_event_url = choose_source_url(block.get("hrefs", []))

        mutable_key = (
            start.isoformat(),
            opponent.lower(),
            location.lower(),
            relation,
        )
        if mutable_key in seen_mutable:
            continue
        seen_mutable.add(mutable_key)

        events.append(
            Event(
                start_local=start,
                end_local=start + timedelta(minutes=DEFAULT_DURATION_MINUTES),
                opponent=opponent,
                location=location,
                relation=relation,
                details=" | ".join(block_lines),
                source_event_url=source_event_url,
                source_order=order,
            )
        )

    events.sort(key=lambda e: (e.start_local, e.source_order))
    assign_stable_uids(events)
    return events


def assign_stable_uids(events: list[Event]) -> None:
    # UIDs deliberately exclude date/time so a rescheduled game updates rather than duplicates.
    # For repeat opponents, the chronological occurrence number disambiguates the events.
    counters: defaultdict[str, int] = defaultdict(int)
    for event in events:
        opponent_key = re.sub(r"[^a-z0-9]+", " ", event.opponent.lower()).strip()
        counters[opponent_key] += 1
        ordinal = counters[opponent_key]

        if event.source_event_url and event.source_event_url != SOURCE_URL:
            base = event.source_event_url
        else:
            base = f"{SEASON_YEAR}|{opponent_key}|{ordinal}"
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
        event.uid = f"{digest}@wl-field-hockey-calendar"


def fallback_blocks_from_schedule_text(body_text: str) -> list[dict]:
    upper = body_text.upper()
    start = upper.find("SCHEDULES")
    if start >= 0:
        end = upper.find("ROSTERS", start)
        segment = body_text[start : end if end >= 0 else None]
    else:
        segment = body_text

    # Split before each date occurrence while retaining the date text.
    matches = list(DATE_RE.finditer(segment))
    blocks: list[dict] = []
    for i, match in enumerate(matches):
        block_start = match.start()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
        text = segment[block_start:block_end].strip()
        if TIME_RE.search(text):
            blocks.append({"text": text, "hrefs": []})
    return blocks


async def scrape_blocks() -> tuple[list[dict], str, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1400})
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        # The current rSchoolToday page presents the schedule behind a Varsity button.
        varsity = page.get_by_role("button", name=re.compile(r"^\s*Varsity\s*$", re.I))
        try:
            count = await varsity.count()
            for i in range(count):
                btn = varsity.nth(i)
                if await btn.is_visible():
                    await btn.click(timeout=5_000)
                    break
        except Exception:
            # The schedule may already be expanded, which is fine.
            pass

        await page.wait_for_timeout(4_000)

        body_text = await page.locator("body").inner_text()
        rendered_html = await page.content()

        # Persist diagnostics in the runner logs/artifacts directory when needed.
        (DEBUG_DIR / "rendered.txt").write_text(body_text, encoding="utf-8")
        (DEBUG_DIR / "rendered.html").write_text(rendered_html, encoding="utf-8")

        blocks = await page.evaluate(
            r"""
            () => {
              const dateRe = /\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?,?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\s+\d{1,2}(?:,?\s+20\d{2})?\b/i;
              const timeRe = /\b\d{1,2}:\d{2}\s*(?:AM|PM)\b/i;
              const normalize = s => (s || '').replace(/\u00a0/g, ' ').trim();
              const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,p,section'));
              const scheduleHeading = all.find(el => normalize(el.textContent).toUpperCase() === 'SCHEDULES');
              const rosterHeading = all.find(el => normalize(el.textContent).toUpperCase() === 'ROSTERS');

              const inScheduleRange = el => {
                if (!scheduleHeading) return true;
                const afterSchedule = !!(scheduleHeading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
                if (!afterSchedule) return false;
                if (!rosterHeading) return true;
                const rosterAfterEl = !!(el.compareDocumentPosition(rosterHeading) & Node.DOCUMENT_POSITION_FOLLOWING);
                return rosterAfterEl;
              };

              const selectors = 'article,li,tr,div,section';
              let matches = Array.from(document.querySelectorAll(selectors)).filter(el => {
                const text = normalize(el.innerText);
                return inScheduleRange(el) && text.length > 0 && text.length < 1800 && dateRe.test(text) && timeRe.test(text);
              });

              // Keep the smallest useful DOM node for each event rather than a parent containing many events.
              matches = matches.filter(el => !Array.from(el.children).some(child => {
                const text = normalize(child.innerText);
                return text.length > 0 && dateRe.test(text) && timeRe.test(text);
              }));

              return matches.map(el => ({
                text: normalize(el.innerText),
                hrefs: Array.from(el.querySelectorAll('a[href]')).map(a => a.href)
              }));
            }
            """
        )
        await browser.close()

    if not blocks:
        blocks = fallback_blocks_from_schedule_text(body_text)
    return blocks, body_text, rendered_html


def ical_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_ical_line(line: str, limit: int = 74) -> str:
    """Fold an iCalendar line to <=75 UTF-8 octets (continuations begin with a space)."""
    raw = line.encode("utf-8")
    if len(raw) <= limit:
        return line

    parts: list[str] = []
    current = bytearray()
    for ch in line:
        b = ch.encode("utf-8")
        max_len = limit if not parts else limit - 1  # account for continuation space
        if current and len(current) + len(b) > max_len:
            parts.append(current.decode("utf-8"))
            current = bytearray()
        current.extend(b)
    if current:
        parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def dt_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: list[Event]) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//W-L Field Hockey Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ical_escape(CALENDAR_NAME)}",
        f"X-WR-TIMEZONE:{LOCAL_TZ_NAME}",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
    ]

    for event in events:
        summary = f"{TEAM_SHORT} {event.relation} {event.opponent}"
        description = (
            f"Automatically generated from {SOURCE_URL}\n"
            f"Scraped details: {event.details}"
        )
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event.uid}",
                f"DTSTAMP:{dt_utc(now)}",
                f"DTSTART:{dt_utc(event.start_local)}",
                f"DTEND:{dt_utc(event.end_local)}",
                f"SUMMARY:{ical_escape(summary)}",
                f"DESCRIPTION:{ical_escape(description)}",
                f"URL:{ical_escape(event.source_event_url or SOURCE_URL)}",
            ]
        )
        if event.location:
            lines.append(f"LOCATION:{ical_escape(event.location)}")
        lines.extend(["STATUS:CONFIRMED", "END:VEVENT"])

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ical_line(line) for line in lines) + "\r\n"


def pages_base_url() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{name}/"


def build_index(events: list[Event]) -> str:
    generated = datetime.now(LOCAL_TZ).strftime("%B %-d, %Y at %-I:%M %p %Z")
    base = pages_base_url()
    calendar_url = f"{base}wl-field-hockey.ics" if base else "wl-field-hockey.ics"
    webcal_url = calendar_url.replace("https://", "webcal://") if calendar_url.startswith("https://") else calendar_url

    rows = []
    for event in events:
        date_text = event.start_local.strftime("%a, %b %-d")
        time_text = event.start_local.strftime("%-I:%M %p")
        rows.append(
            "<tr>"
            f"<td>{html.escape(date_text)}</td>"
            f"<td>{html.escape(time_text)}</td>"
            f"<td>{html.escape(event.relation)} {html.escape(event.opponent)}</td>"
            f"<td>{html.escape(event.location)}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>{html.escape(CALENDAR_NAME)}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 920px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
a.button {{ display: inline-block; padding: .65rem .9rem; border: 1px solid #777; border-radius: .45rem; text-decoration: none; margin-right: .4rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1.5rem; }}
th, td {{ text-align: left; padding: .55rem; border-bottom: 1px solid #ddd; vertical-align: top; }}
.small {{ color: #666; font-size: .92rem; }}
code {{ overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>{html.escape(CALENDAR_NAME)}</h1>
<p>This calendar is generated automatically from the Washington-Liberty athletics schedule and refreshed daily.</p>
<p><a class=\"button\" href=\"{html.escape(webcal_url)}\">Subscribe in Apple Calendar</a>
<a class=\"button\" href=\"{html.escape(calendar_url)}\">Open ICS feed</a></p>
<p><strong>Calendar URL:</strong><br><code>{html.escape(calendar_url)}</code></p>
<p class=\"small\">Google Calendar: Other calendars → + → From URL, then paste the Calendar URL above.<br>
Apple Calendar: use the Subscribe button above, or add a subscription calendar and paste the same URL.</p>
<h2>Current schedule</h2>
<table>
<thead><tr><th>Date</th><th>Time</th><th>Game</th><th>Location</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<p class=\"small\">Last successful generation: {html.escape(generated)}. Source: <a href=\"{html.escape(SOURCE_URL)}\">Washington-Liberty Field Hockey</a>.</p>
</body>
</html>
"""


def write_debug_summary(blocks: list[dict], events: list[Event]) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": SOURCE_URL,
        "season_year": SEASON_YEAR,
        "block_count": len(blocks),
        "event_count": len(events),
        "events": [
            {
                "start_local": e.start_local.isoformat(),
                "opponent": e.opponent,
                "location": e.location,
                "relation": e.relation,
                "source_url": e.source_event_url,
                "uid": e.uid,
                "details": e.details,
            }
            for e in events
        ],
    }
    (DEBUG_DIR / "parsed.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def main() -> int:
    blocks, _, _ = await scrape_blocks()
    events = parse_blocks(blocks)
    write_debug_summary(blocks, events)

    if len(events) < MIN_EVENTS:
        print(
            f"ERROR: Parsed only {len(events)} schedule events; minimum is {MIN_EVENTS}. "
            "Refusing to publish so the previous good calendar remains live.",
            file=sys.stderr,
        )
        print(f"Diagnostics are in {DEBUG_DIR}/", file=sys.stderr)
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    (OUTPUT_DIR / "wl-field-hockey.ics").write_text(build_ics(events), encoding="utf-8", newline="")
    (OUTPUT_DIR / "index.html").write_text(build_index(events), encoding="utf-8")

    print(f"Built {OUTPUT_DIR / 'wl-field-hockey.ics'} with {len(events)} events.")
    for e in events:
        print(f"- {e.start_local:%Y-%m-%d %I:%M %p} {e.relation} {e.opponent} | {e.location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
