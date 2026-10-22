"""Who may deliver a Telegram update to the webhook.

The bot token used to be the URL path, so every request put it into Cloudflare's
analytics and any proxy log on the way. The path is now fixed and public; what proves
the sender is Telegram is the `secret_token` given to setWebhook, which Telegram echoes
in a header on every delivery.
"""
from __future__ import annotations

import hmac
import os

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
SECRET_ENV = "BOT_WEBHOOK_SECRET"


def load_webhook_secret() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def webhook_url(base_url: str) -> str:
    return base_url.rstrip("/") + WEBHOOK_PATH


def webhook_status(path: str, header_secret: str | None, expected_secret: str) -> int:
    """HTTP status for an incoming POST; 200 means the update is Telegram's and gets processed."""
    if path.split("?", 1)[0] != WEBHOOK_PATH:
        return 404
    # An empty expected secret would let an empty header through; with no secret configured nobody gets in.
    if not expected_secret or not header_secret:
        return 401
    if not hmac.compare_digest(header_secret.encode(), expected_secret.encode()):
        return 403
    return 200
