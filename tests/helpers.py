from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

PASSWORD = "correct horse battery"


def make_client() -> TestClient:
    # Secure cookies are only sent over https, so follow the configured public URL scheme.
    scheme = "https" if get_settings().public_base_url.startswith("https://") else "http"
    return TestClient(create_app(), base_url=f"{scheme}://testserver", follow_redirects=False)


def csrf_of(client: TestClient, path: str = "/app") -> str:
    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text) or re.search(r'data-csrf="([^"]+)"', r.text)
    assert m, f"no csrf token on {path} ({r.status_code})"
    return m.group(1)


def register(client: TestClient, email="a@example.com", name="Картинг Уфа", site="") -> TestClient:
    r = client.post(
        "/register",
        data={"email": email, "password": PASSWORD, "name": name, "site_url": site},
    )
    assert r.status_code == 303, r.text[:300]
    return client


def post(client: TestClient, path: str, data: dict | None = None, page: str = "/app", **kw):
    data = dict(data or {})
    data["csrf"] = csrf_of(client, page)
    return client.post(path, data=data, **kw)
