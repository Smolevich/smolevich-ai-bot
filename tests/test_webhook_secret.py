"""Regression: the bot token is no longer the webhook path.

Until 2026-10-22 Telegram posted to `ai.smolevich.com/<bot_id>:<token>`, and the token
showed up in Cloudflare's request analytics. Now the path is fixed and the sender proves
itself with the `X-Telegram-Bot-Api-Secret-Token` header. These tests talk to the real
handler over HTTP.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_spec = importlib.util.spec_from_file_location("bot_webhook", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

SECRET = "s3cret-for-tests_0123456789abcdef"
OLD_TOKEN_PATH = "/123456789:AAFakeTokenFakeTokenFakeTokenFake"


class WebhookAuth(unittest.TestCase):
    def setUp(self):
        self.processed = []
        patches = [
            mock.patch.dict(os.environ, {"BOT_WEBHOOK_SECRET": SECRET}),
            mock.patch.object(bot, "load_bot_token", return_value="123456789:AAFakeTokenFakeTokenFakeTokenFake"),
            mock.patch.object(bot, "load_admin", return_value=1),
            mock.patch.object(bot.executorPool, "submit", side_effect=lambda fn, upd, *a: self.processed.append(upd)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), bot.WebhookHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, secret=None):
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        url = f"http://127.0.0.1:{self.server.server_address[1]}{path}"
        req = urllib.request.Request(url, b'{"update_id": 1}', headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_update_with_the_right_secret_is_processed(self):
        self.assertEqual((self.post("/telegram/webhook", SECRET), self.processed), (200, [{"update_id": 1}]))

    def test_update_without_secret_is_rejected(self):
        self.assertEqual((self.post("/telegram/webhook"), self.processed), (401, []))

    def test_update_with_wrong_secret_is_rejected(self):
        self.assertEqual((self.post("/telegram/webhook", "wrong"), self.processed), (403, []))

    def test_retired_token_path_is_gone_even_with_the_secret(self):
        self.assertEqual((self.post(OLD_TOKEN_PATH, SECRET), self.processed), (404, []))

    def test_no_configured_secret_lets_nobody_in(self):
        with mock.patch.dict(os.environ, {"BOT_WEBHOOK_SECRET": ""}):
            self.assertEqual((self.post("/telegram/webhook", ""), self.processed), (401, []))


class WebhookRegistration(unittest.TestCase):
    def test_set_webhook_sends_a_token_free_url_and_the_secret(self):
        calls = []
        with mock.patch.object(bot, "tg_request", side_effect=lambda t, m, d, **k: calls.append((m, d)) or {"ok": True}):
            bot.register_webhook("123456789:AAFakeTokenFakeTokenFakeTokenFake", SECRET)
        self.assertEqual(calls, [("setWebhook", {"url": bot.TUNNEL_URL.rstrip("/") + "/telegram/webhook", "secret_token": SECRET})])


if __name__ == "__main__":
    unittest.main()
