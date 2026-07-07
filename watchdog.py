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
