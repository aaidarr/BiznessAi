"""Proof of site ownership: a meta tag on the home page or a token file at the site root."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.models import Business
from app.security import const_eq
from app.services.crawler import UnsafeURL, default_resolver, safe_get

META_NAME = "otvet-verify"


def verify_snippets(biz: Business) -> dict[str, str]:
    return {
        "meta": f'<meta name="{META_NAME}" content="{biz.verify_token}">',
        "file": f"otvet-verify-{biz.verify_token}.txt",
    }


def verify_site(biz: Business, client: httpx.Client | None = None, resolver=default_resolver) -> bool:
    if not biz.site_url:
        return False
    s = get_settings()
    own = client is None
    client = client or httpx.Client(timeout=s.crawl_timeout_seconds)
    try:
        try:
            _, _, body = safe_get(client, biz.site_url, s, resolver)
            soup = BeautifulSoup(body, "lxml")
            for m in soup.find_all("meta", attrs={"name": META_NAME}):
                if const_eq(str(m.get("content", "")), biz.verify_token):
                    return True
        except (UnsafeURL, httpx.HTTPError):
            pass
        try:
            url = urljoin(biz.site_url, f"/otvet-verify-{biz.verify_token}.txt")
            _, _, body = safe_get(client, url, s, resolver)
            return const_eq(body.decode("utf-8", "replace").strip(), biz.verify_token)
        except (UnsafeURL, httpx.HTTPError):
            return False
    finally:
        if own:
            client.close()
