from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import httpx

MAX_BYTES = 200_000


def fetch(url: str) -> str:
    try:
        u = urlparse(url)
    except Exception:
        return "<error>bad url</error>"
    if u.scheme not in ("http", "https"):
        return f"<error>scheme not allowed: {u.scheme}</error>"
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": "rasp-agent/0.2"},
        ) as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return f"<error>response too large (cap {MAX_BYTES} bytes)</error>"
                    chunks.append(chunk)
                body = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as e:
        return f"<error>fetch failed: {e}</error>"
    text = _strip_html(body)
    if len(text) > 8000:
        text = text[:8000] + "\n<truncated>"
    return f"<web url={url}>\n{text}\n</web>"


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(source: str) -> str:
    source = re.sub(r"(?is)<script.*?</script>", " ", source)
    source = re.sub(r"(?is)<style.*?</style>", " ", source)
    text = _TAG.sub(" ", source)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    return text.strip()
