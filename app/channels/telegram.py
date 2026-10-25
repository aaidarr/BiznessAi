from __future__ import annotations

import logging

import httpx

from app.channels.clients import get_client

log = logging.getLogger("otvet.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


def _call(token: str, method: str, payload: dict | None = None) -> dict:
    try:
        r = get_client().post(API.format(token=token, method=method), json=payload or {})
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        # never log the URL: it contains the bot token
        raise TelegramError(f"{method}: network error ({type(e).__name__})") from e
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', 'error')}"[:200])
    return data["result"]


def get_me(token: str) -> dict:
    return _call(token, "getMe")


def set_webhook(token: str, url: str, secret: str) -> None:
    _call(
        token,
        "setWebhook",
        {"url": url, "secret_token": secret, "allowed_updates": ["message"], "max_connections": 10},
    )


def delete_webhook(token: str) -> None:
    _call(token, "deleteWebhook")


def send_message(token: str, chat_id: str | int, text: str) -> None:
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text[:4000]})


def parse_update(update: dict) -> tuple[str, str, str] | None:
    """-> (chat_id, text, display_name) for a plain text message, else None."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    chat = msg.get("chat") or {}
    if not isinstance(text, str) or chat.get("type") != "private" or "id" not in chat:
        return None
    frm = msg.get("from") or {}
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    return str(chat["id"]), text, name
