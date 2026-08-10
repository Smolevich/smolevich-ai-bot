"""A provider dropped from PROVIDERS must not break sessions that still name it.

HuggingFace was removed once its free tier ended, but `sessions` rows kept
provider='huggingface' — and every `PROVIDERS[prov]` lookup downstream would
raise KeyError for those users.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_TMP_DB = Path(tempfile.mkdtemp()) / "sessions-test.db"

from agent.config import PROVIDERS, PROVIDER_DEFAULT  # noqa: E402
import agent.db as agent_db  # noqa: E402
from agent.db import DB  # noqa: E402

# Patch the module global rather than BOT_DB_FILE: another test module may have
# imported agent.config already, and the env var is only read at import time.
agent_db.DB_FILE = str(_TMP_DB)

SCHEMA = """
CREATE TABLE sessions (
    user_id INTEGER PRIMARY KEY,
    model TEXT,
    history_json TEXT,
    provider TEXT,
    tools_enabled INTEGER,
    engine_mode TEXT,
    last_session_id TEXT,
    profile TEXT,
    ui_lang TEXT
)
"""


def seed_session(uid, provider, model):
    with sqlite3.connect(_TMP_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, '[]', ?, 1, 'native', '', 'beginner', 'ru')",
            (uid, model, provider),
        )


class RetiredProviderSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with sqlite3.connect(_TMP_DB) as conn:
            conn.execute(SCHEMA)

    def test_retired_provider_falls_back_to_default(self):
        seed_session(1, "huggingface", "openai/gpt-oss-20b:fastest")
        self.assertEqual(DB.get_session(1)["provider"], PROVIDER_DEFAULT)

    def test_retired_provider_does_not_keep_its_model(self):
        seed_session(2, "huggingface", "openai/gpt-oss-20b:fastest")
        self.assertNotEqual(DB.get_session(2)["model"], "openai/gpt-oss-20b:fastest")

    def test_live_provider_is_left_alone(self):
        seed_session(3, "groq", "llama-3.3-70b-versatile")
        sess = DB.get_session(3)
        self.assertEqual(sess["provider"], "groq")
        self.assertEqual(sess["model"], "llama-3.3-70b-versatile")

    def test_huggingface_is_gone_from_providers(self):
        self.assertNotIn("huggingface", PROVIDERS)


if __name__ == "__main__":
    unittest.main()
