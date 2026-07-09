import argparse
import hashlib
import json
import logging
import os
import platform
import random
import re
import smptlib
import subprocess
import time
from datetime import datetime, timezone
from difflib import unified_diff
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import Path

# Setting constant variables
STORE_PATH = Path(os.environ.get(
    "WATCHDOG_FILE", Path.home() / ".watchdog.json"))
USER_AGENT = "watchdog/1.0 (+https://example.com/bot; polite website monitor)"
REQUEST_TIMEOUT = 20
DEFAULT_INTERVAL = 300

log = logging.getLogger("watchdog")  # initializing logger


class Store:
    def __init__(self, path=STORE_PATH):
        self.path = Path(path)
        self.data = {"next_id": 1, "watches": []}
        self.load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.error("could not read %s (%s); starting fresh", self.path, e)
        self.data.setdefault("next_id", 1)
        self.data.setdefault("watches", [])

    def _save(self):
        tmp = self.path.with_suffix("json.tmp")
        tmp.write_text(json.dump(self.data, indent=2))
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

# Fetching and Conent Extraction


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def extract(html: str, selector: str | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    if selector:
        nodes = soup.select(selector)
        if not nodes:
            raise ValueError(f"selector matched  nothing: {selector!r}")
        text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
    else:
        body = soup.body or soup
        text = body.get_text(" ", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()
