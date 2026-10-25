"""Outbound HTTP for channel APIs. Tests replace the client with a mock transport."""

from __future__ import annotations

import httpx

_override: httpx.Client | None = None
_default: httpx.Client | None = None


def set_client(c: httpx.Client | None) -> None:
    global _override
    _override = c


def get_client() -> httpx.Client:
    global _default
    if _override is not None:
        return _override
    if _default is None:
        _default = httpx.Client(timeout=10.0)
    return _default
