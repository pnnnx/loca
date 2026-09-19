"""web_fetch: download a page and return readable text."""
from __future__ import annotations

import html
import re
import urllib.request

from .base import Tool, ToolError, obj, truncate


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|pre)>", "\n", raw)
    raw = re.sub(r"(?i)<li[^>]*>", "- ", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class WebFetch(Tool):
    name = "web_fetch"
    description = "Fetch a URL (docs, API references) and return its text content."
    parameters = obj({"url": {"type": "string"}}, ["url"])
    read_only = False
    kind = "other"

    def permission_key(self, args):
        m = re.match(r"https?://([^/]+)", args.get("url", ""))
        return f"web_fetch({m.group(1) if m else args.get('url', '')})"

    def run(self, args, ctx):
        url = args["url"]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        req = urllib.request.Request(url, headers={"User-Agent": "loca-agent/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ctype = r.headers.get("Content-Type", "")
                body = r.read(2_000_000).decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"fetch failed: {e}")
        text = html_to_text(body) if "html" in ctype else body
        return truncate(text, ctx.cfg["max_tool_output"])
