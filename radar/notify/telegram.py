"""Telegram delivery — free fallback, and two-way if you want to reply.

No priority levels, so severity is carried in the message text instead.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

MAX_MESSAGE_CHARS = 4000  # API hard limit is 4096


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    def available(self) -> bool:
        return bool(self.token and self.chat_id)

    def describe_missing(self) -> str:
        missing = []
        if not self.token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.chat_id:
            missing.append("TELEGRAM_CHAT_ID")
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

        badge = {"critical": "🚨", "high": "⚠️", "medium": "•", "low": "·", "info": "·"}
        text = f"{badge.get(level, '•')} <b>{title}</b>\n\n{message}"
        if url:
            text += f'\n\n<a href="{url}">{url_title or "Open dashboard"}</a>'

        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:MAX_MESSAGE_CHARS],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        api = f"https://api.telegram.org/bot{self.token}/sendMessage"
        request = urllib.request.Request(api, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
                return {"ok": bool(body.get("ok")), "response": body}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": f"network error: {exc}"}
