"""Pushover delivery.

Priority mapping, which is the reason Pushover was chosen over a chat bot:

  info     -> -1  quiet, no sound
  low/med  ->  0  normal push
  high     ->  1  bypasses your phone's quiet hours
  critical ->  2  emergency; repeats until you acknowledge it

Priority 2 requires ``retry`` and ``expire``; Pushover rejects the message
otherwise, so they are always sent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

API_URL = "https://api.pushover.net/1/messages.json"

PRIORITY = {"info": -1, "low": 0, "medium": 0, "high": 1, "critical": 2}

# Emergency alerts re-notify every 5 minutes for up to an hour.
EMERGENCY_RETRY_SECONDS = 300
EMERGENCY_EXPIRE_SECONDS = 3600

MAX_MESSAGE_CHARS = 1024


class PushoverNotifier:
    name = "pushover"

    def __init__(self, token: Optional[str] = None, user: Optional[str] = None) -> None:
        self.token = token or os.environ.get("PUSHOVER_TOKEN", "")
        self.user = user or os.environ.get("PUSHOVER_USER", "")

    def available(self) -> bool:
        return bool(self.token and self.user)

    def describe_missing(self) -> str:
        missing = []
        if not self.token:
            missing.append("PUSHOVER_TOKEN")
        if not self.user:
            missing.append("PUSHOVER_USER")
        return ", ".join(missing)

    def send(
        self,
        title: str,
        message: str,
        level: str = "medium",
        url: Optional[str] = None,
        url_title: Optional[str] = None,
    ) -> Dict[str, object]:
        if not self.available():
            return {"ok": False, "error": f"missing {self.describe_missing()}"}

        priority = PRIORITY.get(level, 0)
        payload: Dict[str, object] = {
            "token": self.token,
            "user": self.user,
            # Pushover truncates silently; trim explicitly so the tail we keep
            # is the one we chose.
            "title": title[:250],
            "message": message[:MAX_MESSAGE_CHARS],
            "priority": priority,
            "html": 1,
        }
        if priority == 2:
            payload["retry"] = EMERGENCY_RETRY_SECONDS
            payload["expire"] = EMERGENCY_EXPIRE_SECONDS
        if url:
            payload["url"] = url
            payload["url_title"] = url_title or "Open dashboard"

        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(API_URL, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
                return {"ok": body.get("status") == 1, "response": body}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": f"network error: {exc}"}
