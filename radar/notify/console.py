"""Console delivery — the default, and what CI logs show."""

from __future__ import annotations

import sys
from typing import Dict, Optional

BADGE = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[low]",
    "info": "[info]",
}


class ConsoleNotifier:
    name = "console"

    def available(self) -> bool:
        return True

    def describe_missing(self) -> str:
        return ""

    def send(
        self,
        title: str,
        message: str,
        level: str = "medium",
        url: Optional[str] = None,
        url_title: Optional[str] = None,
    ) -> Dict[str, object]:
        badge = BADGE.get(level, "[?]")
        print(f"\n{badge} {title}", file=sys.stdout)
        for line in message.splitlines():
            print(f"    {line}", file=sys.stdout)
        if url:
            print(f"    -> {url}", file=sys.stdout)
        return {"ok": True}
