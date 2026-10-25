from __future__ import annotations

import secrets

import httpx

from app.channels.clients import get_client

API = "https://api.vk.com/method/messages.send"
VERSION = "5.199"


class VKError(Exception):
    pass


def send_message(token: str, peer_id: str | int, text: str) -> None:
    try:
        r = get_client().post(
            API,
            data={
                "peer_id": peer_id,
                "random_id": secrets.randbelow(2**31),
                "message": text[:4000],
                "v": VERSION,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise VKError(f"network error ({type(e).__name__})") from e
    if "error" in data:
        raise VKError(str(data["error"].get("error_msg", "error"))[:200])


def parse_event(event: dict) -> tuple[str, str] | None:
    """-> (peer_id, text) for an incoming private message, else None."""
    if event.get("type") != "message_new":
        return None
    msg = (event.get("object") or {}).get("message") or {}
    text, peer = msg.get("text"), msg.get("peer_id")
    # peer_id < 2e9 means a private dialog, not a group chat
    if not isinstance(text, str) or not isinstance(peer, int) or peer >= 2_000_000_000:
        return None
    if msg.get("out"):
        return None
    return str(peer), text
