# watchdog

A small command-line tool that monitors web pages for changes. Point it at a
URL (a whole page, or one element like a price or a status line), and it checks
on a schedule, detects when the content changes, shows you a diff, and alerts
you through any combination of console, email, SMS, webhook, or desktop
notification.

State lives in a plain JSON file you can open and read 

## Requirements

- Python 3.9+
- `requests` and `beautifulsoup4`

```bash
pip install requests beautifulsoup4
```

For desktop notifications on Windows, also `pip install plyer`. On macOS and
Linux the built-in `osascript` / `notify-send` are used.

## Quick start

```bash
# add a page to watch (you'll be prompted for the URL if you omit it)
python watchdog.py add "https://example.com/status" --name "status page"

# watch a single element, e.g. a price, checked every 10 minutes
python watchdog.py add "https://shop.example.com/item" \
    --selector "span.price" --interval 600 --name "widget price"

# run one check pass now
python watchdog.py check

# run continuously, alerting by email and text on any change
python watchdog.py watch --email --sms
```

## Commands

| Command | What it does |
| --- | --- |
| `add [url]` | Register a URL to watch. Omit the URL to be prompted for it. |
| `list` | Show every watch, its settings, and when it last checked/changed. |
| `remove <id>` | Delete a watch by its id (get the id from `list`). |
| `check [url]` | Run one check pass over all watches, then exit. |
| `watch [url]` | Run forever, checking each watch on its own interval. |
| `interactive` | A typed menu — add, list, remove, and check without flags. |

`add`, `check`, and `watch` accept the same options for a new URL:
`--name`, `--selector`, and `--interval` (seconds, default 300).

Passing a URL to `check` or `watch` adds it if it isn't already tracked, then
runs — so `python watchdog.py watch "https://…"` works as a one-liner. If the
URL is already being watched, it isn't duplicated.

If you leave off `--selector`, the whole page body is watched. With a selector,
only the matching element(s) are compared, which is what you want for a single
price or status value. Scripts, styles, and whitespace noise are stripped
before comparison so trivial reformatting doesn't trigger a false alert.

## Alerts

Every run logs changes to the console. Add any of these flags to `check` or
`watch` to also send alerts elsewhere:

| Flag | Channel |
| --- | --- |
| `--email` | Email via SMTP |
| `--sms` | Text message via Twilio |
| `--webhook <url>` | Slack or Discord incoming webhook |
| `--desktop` | Native desktop notification |

Credentials are read from environment variables, never stored in the JSON
file. If you set a flag but a required variable is missing, the tool logs
exactly which ones and skips that channel instead of crashing.

### Email (SMTP)

For Gmail, generate an [App Password](https://support.google.com/accounts/answer/185833)
(a normal login password won't work with 2-factor authentication):

```bash
export WATCHDOG_SMTP_HOST=smtp.gmail.com
export WATCHDOG_SMTP_PORT=587
export WATCHDOG_SMTP_USER=you@gmail.com
export WATCHDOG_SMTP_PASS=your_app_password
export WATCHDOG_EMAIL_FROM=you@gmail.com
export WATCHDOG_EMAIL_TO=you@gmail.com
```

### SMS / phone

Two options:

**Twilio (reliable, real SMS)** — sign up, get a number, then:

```bash
export WATCHDOG_TWILIO_SID=ACxxxxxxxx
export WATCHDOG_TWILIO_TOKEN=your_auth_token
export WATCHDOG_TWILIO_FROM=+15551230000   # your Twilio number
export WATCHDOG_TWILIO_TO=+15559876543     # your phone
```

**Free, no signup** — many carriers accept email-to-SMS. Point
`WATCHDOG_EMAIL_TO` at your carrier's gateway and use `--email` instead of
`--sms`:

| Carrier | Gateway address |
| --- | --- |
| Verizon | `5559876543@vtext.com` |
| AT&T | `5559876543@txt.att.net` |
| T-Mobile | `5559876543@tmomail.net` |

Reliability varies by carrier; Twilio is the dependable route.

## Where data is stored

Watches and their state are kept in `~/.watchdog.json`, written atomically so
an interrupted run can't corrupt it. Override the location with:

```bash
export WATCHDOG_FILE=/path/to/my-watches.json
```

The file is a readable list of watches, each recording the URL, selector,
interval, and the last snapshot/hash used to detect changes.

## Running on a schedule

`watch` runs in the foreground and is tied to your terminal. For unattended
monitoring, the more robust pattern is to run `check` on a scheduler:

- **cron** (macOS/Linux): `*/15 * * * * cd /path/to/tool && python watchdog.py check --email`
- **Task Scheduler** (Windows): run `python watchdog.py check --email` on a timer.

This survives reboots and doesn't depend on a long-lived process.

## Limitations and caveats

**JavaScript-rendered pages.** The tool fetches raw HTML. If a page loads its
content with JavaScript (common for prices, dashboards, and single-page apps),
that content won't be in what's fetched. Check "View Source" in your browser —
if the value you want isn't there, you'll need a real browser engine like
Playwright rather than a plain HTTP request.

**Anti-scraping / bot detection.** Large sites — Amazon especially — actively
block automated requests. They may return a CAPTCHA or "robot check" page (often
with a normal 200 status) instead of the real content. The tool detects common
block pages and refuses to treat them as a valid baseline, warning you instead,
but it can't bypass the block. For heavily protected sites, use the site's
official API where one exists, or a purpose-built service.

**Be a good citizen.** The tool sends browser-like headers and adds a short
randomized delay between requests, but you should still check each site's
`robots.txt` and Terms of Service, keep intervals reasonable, and prefer an
official API when one is available. Some sites prohibit automated access.

## How change detection works

For each check, the tool fetches the page, extracts the watched text (whole
body, or a CSS-selected element), normalizes whitespace, and hashes it. The
first check records a baseline silently. On later checks, if the hash differs
from the stored one, it builds a unified diff, sends alerts, and saves the new
snapshot. Identical content is quietly skipped, and network errors or block
pages are logged without overwriting a good baseline.
