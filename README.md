# W-L Varsity Field Hockey Calendar

This repository turns the public Washington-Liberty High School varsity field hockey schedule into a subscribable iCalendar (`.ics`) feed.

Source schedule:
`https://www.wlgeneralsathletics.com/fall-sports/field-hockey/`

## What the workflow does

1. Runs Chromium headlessly with Playwright because the rSchoolToday/Arbiter schedule is injected dynamically.
2. Opens the Field Hockey page and expands the **Varsity** schedule.
3. Extracts game date/time, opponent, and location using text/DOM heuristics.
4. Creates `site/wl-field-hockey.ics` and a small `site/index.html` subscription page.
5. Refuses to deploy if it finds fewer than the configured minimum number of games, protecting the last good feed if the website changes.
6. Deploys the generated `site/` directory to GitHub Pages.
7. Saves an audit copy under `published/` and commits it back to the repository.
8. Repeats every day at **6:17 AM America/New_York** and can also be run manually.

## One-time GitHub setup

1. Create a repository, for example `wl-field-hockey-calendar`.
2. Copy this project into the repository and push it to the default branch (normally `main`).
3. In **Settings → Pages**, set **Source** to **GitHub Actions**.
4. Open **Actions → Refresh and publish field hockey calendar → Run workflow** for the first build.
5. When it finishes, open **Settings → Pages** (or the workflow deployment link) to get the Pages URL.

For a repository named `wl-field-hockey-calendar` under GitHub user `YOURNAME`, the feed will normally be:

`https://YOURNAME.github.io/wl-field-hockey-calendar/wl-field-hockey.ics`

## Subscribe

### Apple Calendar / iPhone / iPad / Mac
Use the subscription button on the generated GitHub Pages landing page, or add a subscription calendar and paste the HTTPS feed URL.

### Google Calendar
On desktop: **Other calendars → + → From URL**, then paste the HTTPS feed URL.

## Configuration

Edit the environment variables in `.github/workflows/refresh-calendar.yml`:

- `SEASON_YEAR`: update this each fall (currently `2026`).
- `MIN_EVENTS`: safety threshold; default `3`.
- `DEFAULT_DURATION_MINUTES`: game length shown on subscribed calendars; default `120` minutes.
- `SOURCE_URL`: the W-L team page.

The cron schedule is also in that workflow file. It currently runs at **6:17 AM Eastern** every day.

## How reschedules are handled

The generated ICS uses a stable UID based primarily on the opponent (or a source event URL when available), not the game's date/time. That means changing a game's date or start time should update the existing subscribed event rather than create a second copy.

## Diagnostics

If a scrape fails, the Actions log will report the problem. The workflow also uploads a 7-day `scraper-diagnostics` artifact. During the run the scraper writes:

- `debug/rendered.txt`
- `debug/rendered.html`
- `debug/parsed.json`

If rSchoolToday changes its markup, these files make it straightforward to adjust the parser.

## Notes

- The public schedule is the source of truth; this project does not edit the school schedule.
- The generated calendar is read-only for subscribers.
- Calendar apps decide when to poll subscribed feeds, so an update published to GitHub may not appear on every device instantly.
