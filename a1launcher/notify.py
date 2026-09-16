"""Optional Telegram notification on a successful launch."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("a1launcher.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 15


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send `text` to a Telegram chat. Returns True on success.

    Notification failure is never fatal — the instance already exists by the
    time this runs, so we log and move on.
    """
    if not token or not chat_id:
        logger.warning("Telegram enabled but bot token or chat id is empty; skipping")
        return False

    try:
        response = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return False

    logger.info("Telegram notification sent to chat %s", chat_id)
    return True
