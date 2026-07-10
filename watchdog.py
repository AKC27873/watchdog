#!/usr/bin/env python3
"""
watchdog.py — a small website change monitor.

Watches one or more URLs (whole page or a specific CSS-selected element),
detects when the content changes, shows a diff, and alerts you via any
combination of: console, email, SMS (phone), webhook, and desktop notification.

State is stored in a plain JSON file (default: ~/.watchdog.json), so you can
open it and read it directly. Only requests + beautifulsoup4 are needed.

    pip install requests beautifulsoup4

Adding + running
----------------
    python watchdog.py add                 # prompts you for the URL
    python watchdog.py add https://site/x --selector "span.price" --interval 600
    python watchdog.py list
    python watchdog.py check --email        # one pass, email me on change
    python watchdog.py watch --email --sms  # run forever, email + text me
    python watchdog.py interactive          # typed menu, no flags

Email / SMS credentials come from environment variables (see NOTIFY SETUP at
the bottom of this file). The JSON file only holds your watches and their state.
"""

import argparse
import hashlib
import json
import logging
import os
import platform
import random
import re
import smtplib
import subprocess
import time
from datetime import datetime, timezone
from difflib import unified_diff
from email.message import EmailMessage
from pathlib import Path

from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

STORE_PATH = Path(os.environ.get(
    "WATCHDOG_FILE", Path.home() / ".watchdog.json"))
# A real browser UA. The default python-requests UA is blocked on sight by
# Amazon and many other sites, so we look like an ordinary browser instead.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)
REQUEST_TIMEOUT = 20
DEFAULT_INTERVAL = 300

# Content that looks like a page but is really a bot-check/CAPTCHA. Amazon and
# others return these with HTTP 200, so we detect them by content, not status.
BLOCK_MARKERS = (
    "robot check",
    "enter the characters you see below",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "api-services-support@amazon.com",
    "sorry, we just need to make sure you're not a robot",
    "request could not be satisfied",  # CloudFront block page
)

log = logging.getLogger("watchdog")


# --------------------------------------------------------------------------- #
# Storage — a JSON file, written atomically
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path=STORE_PATH):
        self.path = Path(path)
        self.data = {"next_id": 1, "watches": []}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.error("could not read %s (%s); starting fresh", self.path, e)
        self.data.setdefault("next_id", 1)
        self.data.setdefault("watches", [])

    def _save(self):
        # write to a temp file then rename, so a crash never corrupts the store
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def add(self, name, url, selector, interval):
        wid = self.data["next_id"]
        self.data["next_id"] += 1
        self.data["watches"].append({
            "id": wid, "name": name, "url": url, "selector": selector,
            "interval": interval, "last_hash": None, "last_text": None,
            "last_check": None, "last_change": None,
        })
        self._save()
        return wid

    def all(self):
        return list(self.data["watches"])

    def get(self, wid):
        return next((w for w in self.data["watches"] if w["id"] == wid), None)

    def remove(self, wid):
        before = len(self.data["watches"])
        self.data["watches"] = [
            w for w in self.data["watches"] if w["id"] != wid]
        self._save()
        return len(self.data["watches"]) < before

    def update(self, wid, **fields):
        w = self.get(wid)
        if w:
            w.update(fields)
            self._save()


# --------------------------------------------------------------------------- #
# Fetching + content extraction
# --------------------------------------------------------------------------- #
def fetch(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def looks_blocked(html: str) -> bool:
    """True if the page is really a CAPTCHA / bot-check served with HTTP 200."""
    lowered = html[:5000].lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def derive_name(url: str) -> str:
    """Make a readable name from a URL, e.g. an Amazon product slug."""
    parsed = urlparse(url)
    skip = {"dp", "gp", "product", "ref", "www"}
    segs = [s for s in parsed.path.split("/") if s and s not in skip]
    if segs:
        name = segs[0].replace("-", " ").replace("_", " ").strip()
        if name:
            return name[:60]
    return parsed.netloc or url


def extract(html: str, selector: str | None) -> str:
    """Normalized text we compare: script/style stripped, whitespace collapsed."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    if selector:
        nodes = soup.select(selector)
        if not nodes:
            raise ValueError(f"selector matched nothing: {selector!r}")
        text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
    else:
        body = soup.body or soup
        text = body.get_text(" ", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def notify_console(title, body):
    log.info("CHANGE: %s\n%s", title, body)


def notify_email(cfg, title, body):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=REQUEST_TIMEOUT) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        log.debug("email sent to %s", cfg["to"])
    except Exception as e:  # noqa: BLE001 - a bad alert must not crash the run
        log.error("email failed: %s", e)


def notify_sms(cfg, title, body):
    """Send a text via Twilio's REST API (no SDK needed, just requests)."""
    text = f"{title}\n{body}"[:300]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{
        cfg['sid']}/Messages.json"
    try:
        resp = requests.post(
            url,
            data={"From": cfg["from"], "To": cfg["to"], "Body": text},
            auth=(cfg["sid"], cfg["token"]),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        log.debug("sms sent to %s", cfg["to"])
    except requests.RequestException as e:
        log.error("sms failed: %s", e)


def notify_webhook(url, title, body):
    message = f"*{title}*\n{body}"
    try:
        resp = requests.post(
            url, json={"text": message, "content": message[:1900]},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("webhook failed: %s", e)


def notify_desktop(title, body):
    system = platform.system()
    safe = body.replace('"', "'").splitlines()[0][:200]
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe}" with title "{title}"'],
                           check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", title, safe], check=False)
        elif system == "Windows":
            from plyer import notification
            notification.notify(title=title, message=safe, timeout=10)
        else:
            log.warning("desktop notifications unsupported on %s", system)
    except FileNotFoundError:
        log.warning(
            "desktop notify tool not found (install notify-send / plyer)")
    except Exception as e:  # noqa: BLE001
        log.warning("desktop notify failed: %s", e)


class Notifier:
    """Fan-out to whichever channels are enabled for this run."""

    def __init__(self, console=True, email=None, sms=None, webhook=None, desktop=False):
        self.console = console
        self.email = email
        self.sms = sms
        self.webhook = webhook
        self.desktop = desktop

    def send(self, title, body):
        if self.console:
            notify_console(title, body)
        if self.email:
            notify_email(self.email, title, body)
        if self.sms:
            notify_sms(self.sms, title, body)
        if self.webhook:
            notify_webhook(self.webhook, title, body)
        if self.desktop:
            notify_desktop(title, body)


def _env_config(mapping):
    """mapping: {result_key: ENV_VAR}. Returns (cfg, missing_env_var_names)."""
    cfg, missing = {}, []
    for key, env in mapping.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
        else:
            missing.append(env)
    return (cfg, []) if not missing else (None, missing)


EMAIL_ENV = {
    "host": "WATCHDOG_SMTP_HOST", "port": "WATCHDOG_SMTP_PORT",
    "user": "WATCHDOG_SMTP_USER", "password": "WATCHDOG_SMTP_PASS",
    "from": "WATCHDOG_EMAIL_FROM", "to": "WATCHDOG_EMAIL_TO",
}
SMS_ENV = {
    "sid": "WATCHDOG_TWILIO_SID", "token": "WATCHDOG_TWILIO_TOKEN",
    "from": "WATCHDOG_TWILIO_FROM", "to": "WATCHDOG_TWILIO_TO",
}


def build_notifier(args):
    email_cfg = None
    if getattr(args, "email", False):
        email_cfg, missing = _env_config(EMAIL_ENV)
        if email_cfg is None:
            log.error("--email set but missing env vars: %s",
                      ", ".join(missing))
        else:
            email_cfg["port"] = int(email_cfg["port"])

    sms_cfg = None
    if getattr(args, "sms", False):
        sms_cfg, missing = _env_config(SMS_ENV)
        if sms_cfg is None:
            log.error("--sms set but missing env vars: %s", ", ".join(missing))

    return Notifier(
        console=True, email=email_cfg, sms=sms_cfg,
        webhook=getattr(args, "webhook", None),
        desktop=getattr(args, "desktop", False),
    )


# --------------------------------------------------------------------------- #
# Core check logic
# --------------------------------------------------------------------------- #
def make_diff(old, new, name):
    diff = unified_diff(old.splitlines(), new.splitlines(),
                        fromfile=f"{name} (before)", tofile=f"{name} (after)",
                        lineterm="", n=1)
    lines = list(diff)
    if len(lines) > 40:
        lines = lines[:40] + ["... (diff truncated) ..."]
    return "\n".join(lines)


def ensure_watch(store, url, name=None, selector=None, interval=DEFAULT_INTERVAL):
    """Return the watch for `url`, adding it first if it isn't tracked yet."""
    for w in store.all():
        if w["url"] == url:
            return w
    label = name or derive_name(url)
    wid = store.add(label, url, selector, interval)
    log.info("added watch: %s", label)
    return store.get(wid)


def check_one(store, watch, notifier):
    """Check a single watch. Returns True if the content changed."""
    name = watch["name"] or watch["url"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        html = fetch(watch["url"])
        if looks_blocked(html):
            log.warning("%s: got a bot-check/CAPTCHA page, not real content — "
                        "skipping (site is blocking the scraper)", name)
            store.update(watch["id"], last_check=now)
            return False
        text = extract(html, watch["selector"])
    except Exception as e:  # noqa: BLE001
        log.error("check failed for %s: %s", name, e)
        store.update(watch["id"], last_check=now)
        return False

    new_hash = content_hash(text)
    first_time = watch["last_hash"] is None
    changed = not first_time and new_hash != watch["last_hash"]

    if changed:
        body = make_diff(watch["last_text"] or "", text, name)
        notifier.send(f"{name} changed", f"{watch['url']}\n\n{body}")
        store.update(watch["id"], last_hash=new_hash, last_text=text,
                     last_check=now, last_change=now)
    else:
        log.info("baseline captured: %s" if first_time else "no change: %s", name)
        store.update(watch["id"], last_hash=new_hash,
                     last_text=text, last_check=now)
    return changed


def due_watches(store):
    now = time.time()
    for w in store.all():
        if w["last_check"] is None:
            yield w
            continue
        try:
            last = datetime.fromisoformat(w["last_check"]).timestamp()
        except ValueError:
            yield w
            continue
        if now - last >= w["interval"]:
            yield w


# --------------------------------------------------------------------------- #
# Interactive input
# --------------------------------------------------------------------------- #
def _ask(prompt, default=None, required=False):
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default
        if not value:
            if required:
                print("  (required — please enter a value)")
                continue
            return default
        return value


def prompt_for_watch():
    print("\nAdd a website to monitor:")
    url = _ask("  URL to watch", required=True)
    name = _ask("  Friendly name (optional)")
    selector = _ask(
        "  CSS selector for one element (optional, blank = whole page)")
    while True:
        raw = _ask("  Check interval in seconds",
                   default=str(DEFAULT_INTERVAL))
        try:
            return name, url, selector, int(raw)
        except ValueError:
            print("  (please enter a whole number of seconds)")


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def cmd_add(args):
    store = Store()
    if args.url is None:
        name, url, selector, interval = prompt_for_watch()
    else:
        name, url, selector, interval = args.name, args.url, args.selector, args.interval
    store.add(name, url, selector, interval)
    print(f"added watch: {name or url}")


def cmd_list(args):
    store = Store()
    rows = store.all()
    if not rows:
        print("no watches yet — add one with `watchdog.py add`")
        return
    for w in rows:
        sel = f"  selector={w['selector']}" if w["selector"] else ""
        print(f"[{w['id']}] {w['name'] or w['url']}")
        print(f"     {w['url']}{sel}")
        print(f"     every {w['interval']}s | last check: {w['last_check'] or 'never'}"
              f" | last change: {w['last_change'] or '—'}")


def cmd_remove(args):
    store = Store()
    print("removed" if store.remove(args.id)
          else f"no watch with id {args.id}")


def cmd_check(args):
    store = Store()
    notifier = build_notifier(args)
    if getattr(args, "url", None):
        ensure_watch(store, args.url, args.name, args.selector, args.interval)
    watches = store.all()
    if not watches:
        print("no watches configured")
        return
    changed = 0
    for w in watches:
        if check_one(store, w, notifier):
            changed += 1
        time.sleep(random.uniform(0.5, 1.5))
    log.info("done: %d watch(es), %d change(s)", len(watches), changed)


def cmd_watch(args):
    store = Store()
    notifier = build_notifier(args)
    if getattr(args, "url", None):
        ensure_watch(store, args.url, args.name, args.selector, args.interval)
    if not store.all():
        print("no watches configured — add one, or pass a URL: "
              "`watchdog.py watch <url>`")
        return
    log.info("watching... (Ctrl-C to stop)")
    try:
        while True:
            for w in due_watches(store):
                check_one(store, w, notifier)
                time.sleep(random.uniform(0.5, 1.5))
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log.info("stopped")


def cmd_interactive(args):
    menu = ("\n=== watchdog ===\n"
            "  1) add a website to watch\n"
            "  2) list watches\n"
            "  3) remove a watch\n"
            "  4) check all now\n"
            "  5) quit\n")
    while True:
        print(menu)
        choice = _ask("Choose an option", default="4")
        if choice == "1":
            store = Store()
            name, url, selector, interval = prompt_for_watch()
            store.add(name, url, selector, interval)
            print(f"added watch: {name or url}")
        elif choice == "2":
            cmd_list(args)
        elif choice == "3":
            cmd_list(args)
            wid = _ask("  id to remove")
            if wid and wid.isdigit():
                args.id = int(wid)
                cmd_remove(args)
        elif choice == "4":
            cmd_check(args)
        elif choice in ("5", "q", "quit", "exit"):
            print("bye")
            return
        else:
            print("  (pick 1-5)")


def main():
    parser = argparse.ArgumentParser(
        description="Monitor websites for changes.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a watch (prompts if no URL given)")
    a.add_argument("url", nargs="?", help="URL to watch; omit to be prompted")
    a.add_argument("--name", help="friendly label")
    a.add_argument("--selector", help="CSS selector to watch one element")
    a.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help="seconds between checks (default 300)")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list watches")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="remove a watch by id")
    r.add_argument("id", type=int)
    r.set_defaults(func=cmd_remove)

    def add_alert_flags(p):
        p.add_argument("--email", action="store_true",
                       help="send email alerts")
        p.add_argument("--sms", action="store_true",
                       help="send SMS (phone) alerts")
        p.add_argument("--webhook", help="Slack/Discord webhook URL")
        p.add_argument("--desktop", action="store_true",
                       help="desktop notifications")

    def add_url_flags(p):
        # optional URL: if given, add-it-if-new then run
        p.add_argument("url", nargs="?", help="URL to add-if-new, then run")
        p.add_argument("--name", help="friendly label for a new URL")
        p.add_argument("--selector", help="CSS selector for a new URL")
        p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                       help="interval for a new URL (default 300s)")

    c = sub.add_parser(
        "check", help="run one check pass (optionally add a URL first)")
    add_url_flags(c)
    add_alert_flags(c)
    c.set_defaults(func=cmd_check)

    w = sub.add_parser(
        "watch", help="run continuously (optionally add a URL first)")
    add_url_flags(w)
    add_alert_flags(w)
    w.add_argument("--poll", type=int, default=30,
                   help="seconds between scheduling passes")
    w.set_defaults(func=cmd_watch)

    i = sub.add_parser("interactive", help="typed menu, no flags needed")
    add_alert_flags(i)
    i.set_defaults(func=cmd_interactive)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    args.func(args)


if __name__ == "__main__":
    main()

# ============================================================================ #
# NOTIFY SETUP — set these environment variables before using --email / --sms
# ============================================================================ #
#
# EMAIL (SMTP). Example for Gmail (use an "App Password", not your login one):
#   export WATCHDOG_SMTP_HOST=smtp.gmail.com
#   export WATCHDOG_SMTP_PORT=587
#   export WATCHDOG_SMTP_USER=you@gmail.com
#   export WATCHDOG_SMTP_PASS=your_app_password
#   export WATCHDOG_EMAIL_FROM=you@gmail.com
#   export WATCHDOG_EMAIL_TO=you@gmail.com
#
# PHONE — option A: real SMS via Twilio (sign up, get a number + credentials):
#   export WATCHDOG_TWILIO_SID=ACxxxxxxxx
#   export WATCHDOG_TWILIO_TOKEN=your_auth_token
#   export WATCHDOG_TWILIO_FROM=+15551230000   # your Twilio number
#   export WATCHDOG_TWILIO_TO=+15559876543     # your phone
#
# PHONE — option B (free, no signup): text yourself THROUGH email.
#   Most carriers accept email-to-SMS. Just point WATCHDOG_EMAIL_TO at your
#   carrier's gateway address and use --email:
#     Verizon:  5559876543@vtext.com
#     AT&T:     5559876543@txt.att.net
#     T-Mobile: 5559876543@tmomail.net
#   (Reliability varies by carrier; Twilio is the dependable route.)
# ============================================================================ #
