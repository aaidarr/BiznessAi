"""Polite, SSRF-hardened site crawler."""

from __future__ import annotations

import ipaddress
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from app.config import Settings, get_settings

UA = "OtvetBot/1.0 (+https://otvet.example/bot)"
MAX_REDIRECTS = 3
_SKIP_EXT = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".rar",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".mp3",
    ".mp4",
    ".avi",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".woff",
)
_BLOCKS = ["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dt", "dd", "address", "blockquote", "figcaption", "summary"]
_NOISE = ["script", "style", "noscript", "nav", "form", "svg", "iframe", "template", "button"]


class UnsafeURL(Exception):
    pass


def is_public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


def default_resolver(host: str) -> list[str]:
    try:
        return sorted({str(ai[4][0]) for ai in socket.getaddrinfo(host, None)})
    except socket.gaierror as e:
        raise UnsafeURL("host does not resolve") from e


def validate_url(url: str, s: Settings, resolver: Callable[[str], list[str]] = default_resolver) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    if p.username or p.password:
        raise UnsafeURL("credentials in URL are not allowed")
    if not s.crawl_allow_private_hosts:
        port = p.port or (443 if p.scheme == "https" else 80)
        if port not in (80, 443):
            raise UnsafeURL("only ports 80/443 are allowed")
        ips = [p.hostname] if _is_ip_literal(p.hostname) else resolver(p.hostname)
        if not ips or not all(is_public_ip(i) for i in ips):
            raise UnsafeURL("address is not public")
    return url


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass
class Page:
    url: str
    title: str
    sections: list[tuple[str, str]]  # (heading, text)
    links: list[str]


def safe_get(client: httpx.Client, url: str, s: Settings, resolver=default_resolver) -> tuple[str, str, bytes]:
    """GET with manual redirects, size cap and a post-connect check of the peer address."""
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url, s, resolver)
        with client.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=False) as r:
            if not s.crawl_allow_private_hosts:
                stream = r.extensions.get("network_stream")
                peer = stream.get_extra_info("server_addr") if stream else None
                if not peer or not is_public_ip(str(peer[0])):
                    raise UnsafeURL("connected peer is not public")
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    raise UnsafeURL("redirect without location")
                url = urljoin(url, loc)
                continue
            if r.status_code != 200:
                raise UnsafeURL(f"http {r.status_code}")
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text/plain" not in ctype:
                raise UnsafeURL("unsupported content type")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > s.crawl_max_bytes:
                    raise UnsafeURL("page too large")
            return url, ctype, bytes(buf)
    raise UnsafeURL("too many redirects")


def parse_page(url: str, html: bytes) -> Page:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    links = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        links.append(urldefrag(urljoin(url, href))[0])
    extras = []
    desc = soup.find("meta", attrs={"name": "description"})
    if isinstance(desc, Tag) and desc.get("content"):
        extras.append(str(desc["content"]).strip())
    for a in soup.find_all("a", href=True):
        h = str(a["href"])
        if h.startswith("tel:"):
            extras.append("Телефон: " + h[4:].strip())
        elif h.startswith("mailto:"):
            extras.append("Email: " + h[7:].split("?")[0].strip())
    for t in soup(_NOISE):
        t.decompose()

    sections: list[tuple[str, list[str]]] = [(title, [])]
    seen_blocks: set[int] = set()
    body = soup.body or soup
    for el in body.find_all(_BLOCKS + ["div", "span"]):
        if id(el) in seen_blocks or any(id(p) in seen_blocks for p in el.parents):
            continue
        if el.name in ("div", "span"):
            has_block = el.find(_BLOCKS + ["div"]) is not None
            direct = "".join(c for c in el.children if isinstance(c, NavigableString)).strip()
            if has_block or len(direct) < 25:
                continue
            text = " ".join(direct.split())
        else:
            text = el.get_text(" ", strip=True)
        text = " ".join(text.split())
        if len(text) < 3:
            continue
        seen_blocks.add(id(el))
        if el.name in ("h1", "h2", "h3", "h4"):
            sections.append((text[:200], [text]))
        else:
            sections[-1][1].append(text)
    if extras:
        sections.append(("Контакты", list(dict.fromkeys(extras))))
    out = [(h, "\n".join(lines)) for h, lines in sections if lines and "\n".join(lines).strip()]
    return Page(url, title, out, links)


def crawl_site(
    start_url: str,
    s: Settings | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[[str], list[str]] = default_resolver,
) -> list[tuple[str, str, str]]:
    """Return [(source_url, heading, text)]. Raises UnsafeURL for an unsafe start URL."""
    s = s or get_settings()
    validate_url(start_url, s, resolver)
    own = client is None
    client = client or httpx.Client(timeout=s.crawl_timeout_seconds)
    try:
        host = (urlparse(start_url).hostname or "").removeprefix("www.")
        robots = RobotFileParser()
        try:
            _, _, rb = safe_get(client, urljoin(start_url, "/robots.txt"), s, resolver)
            robots.parse(rb.decode("utf-8", "replace").splitlines())
        except Exception:
            robots.parse([])
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        seen = {urldefrag(start_url)[0]}
        results: list[tuple[str, str, str]] = []
        seen_text: set[str] = set()
        t0 = time.monotonic()
        pages = 0
        while queue and pages < s.crawl_max_pages and time.monotonic() - t0 < s.crawl_total_seconds:
            url, depth = queue.popleft()
            if not robots.can_fetch(UA, url):
                continue
            try:
                final, _, body = safe_get(client, url, s, resolver)
            except (UnsafeURL, httpx.HTTPError):
                continue
            pages += 1
            page = parse_page(final, body)
            for heading, text in page.sections:
                key = text.strip().lower()
                if key in seen_text:
                    continue
                seen_text.add(key)
                results.append((final, heading, text))
            if depth < s.crawl_max_depth:
                for link in page.links:
                    lp = urlparse(link)
                    if (lp.hostname or "").removeprefix("www.") != host:
                        continue
                    if lp.path.lower().endswith(_SKIP_EXT) or link in seen:
                        continue
                    seen.add(link)
                    queue.append((link, depth + 1))
        return results
    finally:
        if own:
            client.close()
