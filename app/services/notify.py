from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.channels import telegram, vk
from app.config import get_settings
from app.models import Business
from app.security import decrypt

log = logging.getLogger("otvet.notify")


def notify_owner(db: Session, biz: Business, text: str) -> None:
    """Best effort: Telegram first, SMTP as a second route. Never raises."""
    if biz.tg_token_enc and biz.tg_owner_chat_id:
        try:
            telegram.send_message(decrypt(biz.tg_token_enc), biz.tg_owner_chat_id, text)
            return
        except Exception as e:
            log.warning("owner telegram notify failed: %s", type(e).__name__)
    s = get_settings()
    if s.smtp_host and biz.owner and biz.owner.email:
        try:
            msg = EmailMessage()
            msg["Subject"] = "Новое уведомление"
            msg["From"] = s.smtp_from or s.smtp_user
            msg["To"] = biz.owner.email
            msg.set_content(text)
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        except Exception as e:
            log.warning("owner email notify failed: %s", type(e).__name__)


def send_to_client(biz: Business, channel: str, external_id: str, text: str) -> bool:
    """Push a message to a client (reminders). Widget clients cannot be pushed to."""
    try:
        if channel == "telegram" and biz.tg_token_enc:
            telegram.send_message(decrypt(biz.tg_token_enc), external_id, text)
            return True
        if channel == "vk" and biz.vk_token_enc:
            vk.send_message(decrypt(biz.vk_token_enc), external_id, text)
            return True
    except Exception as e:
        log.warning("client push failed (%s): %s", channel, type(e).__name__)
    return False
