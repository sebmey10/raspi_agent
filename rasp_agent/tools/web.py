from __future__ import annotations

import re
import subprocess
from urllib.parse import urlparse

MAX_BYTES = 200_000


def fetch(url: str) -> str:
    try:
        u = urlparse(url)
    except Exception:
        return "<error>bad url</error>"
    if u.scheme not in ("http", "https"):
        return f"<error>scheme not allowed: {u.scheme}</error>"
    try:
        proc = subprocess.run(
            ["curl", "-sL", "-A", "rasp-agent/0.1", "--max-time", "20",
             "--max-filesize", str(MAX_BYTES), url],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        return "<error>fetch timeout</error>"
    body = proc.stdout or ""
    if proc.returncode != 0:
        return f"<error>curl exit {proc.returncode}: {(proc.stderr or '').strip()}</error>"
    text = _strip_html(body)
    if len(text) > 8000:
        text = text[:8000] + "\n<truncated>"
    return f"<web url={url}>\n{text}\n</web>"


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<script.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    text = _TAG.sub(" ", html)
    text = _WS.sub(" ", text)
    return text.strip()
