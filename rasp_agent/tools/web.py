from __future__ import annotations

import html
import ipaddress
import os
import re
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 200_000


def fetch(url: str) -> str:
    err = _validate_url(url)
    if err:
        return err
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=20,
            headers={"User-Agent": "rasp-agent/0.2"},
        ) as client:
            current = url
            for _ in range(6):
                err = _validate_url(current)
                if err:
                    return err
                with client.stream("GET", current) as r:
                    if 300 <= r.status_code < 400 and "location" in r.headers:
                        current = urljoin(current, r.headers["location"])
                        continue
                    r.raise_for_status()
                    body = _limited_body(r)
                    url = str(r.url)
                    break
            else:
                return "<error>too many redirects</error>"
    except httpx.HTTPError as e:
        return f"<error>fetch failed: {e}</error>"
    text = _strip_html(body)
    if len(text) > 8000:
        text = text[:8000] + "\n<truncated>"
    return f"<web url={url}>\n{text}\n</web>"


def _limited_body(response: httpx.Response) -> str:
    total = 0
    chunks: list[bytes] = []
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            break
        chunks.append(chunk)
    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    if total > MAX_BYTES:
        body += "\n<truncated>"
    return body


def _validate_url(url: str) -> str | None:
    try:
        u = urlparse(url)
    except Exception:
        return "<error>bad url</error>"
    if u.scheme not in ("http", "https"):
        return f"<error>scheme not allowed: {u.scheme}</error>"
    host = u.hostname
    if not host:
        return "<error>missing host</error>"
    if _private_web_allowed():
        return None
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".localhost"):
        return "<error>private/local web targets are denied by default</error>"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return "<error>private/local web targets are denied by default</error>"
    return None


def _private_web_allowed() -> bool:
    return os.environ.get("RASP_WEB_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes", "on"}


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(source: str) -> str:
    source = re.sub(r"(?is)<script.*?</script>", " ", source)
    source = re.sub(r"(?is)<style.*?</style>", " ", source)
    text = _TAG.sub(" ", source)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    return text.strip()
