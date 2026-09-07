"""Regression: a model the provider stopped listing must not stay marked alive.

The sweep writes rows only for models it finds in the provider's /v1/models, so a
delisted model was simply never touched again and its row froze. On 2026-09-01 NVIDIA
dropped nvidia/nemotron-3-nano-30b-a3b; the frozen row kept being handed to people and
answered HTTP 410 for six days.

Stdlib only, like the rest of the project. `model-health-check.py` is hyphenated and
cannot be imported normally, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_spec = importlib.util.spec_from_file_location("model_health_check", _BOT_DIR / "model-health-check.py")
assert _spec and _spec.loader
mhc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mhc)

RUN_START = 1_757_260_000
SCHEMA = """
CREATE TABLE model_health (
    provider TEXT, model_id TEXT, latency_ms INTEGER, available INTEGER,
    supports_tools INTEGER, category TEXT, last_check INTEGER, capabilities TEXT DEFAULT '',
    PRIMARY KEY (provider, model_id)
)
"""


def db_with(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO model_health (provider, model_id, latency_ms, available, supports_tools, category, last_check) "
        "VALUES (?, ?, 10, ?, 1, ?, ?)", rows)
    conn.commit()
    return conn


def availability(conn, model_id):
    return conn.execute("SELECT available FROM model_health WHERE model_id = ?", (model_id,)).fetchone()[0]


class DelistedModelsAreRetired(unittest.TestCase):
    def test_a_model_the_sweep_did_not_touch_is_marked_unavailable(self):
        conn = db_with([("nvidia", "nemotron-3-nano", 1, "text", RUN_START - 6 * 86400)])
        mhc.retire_unseen_models(conn, "nvidia", RUN_START)
        self.assertEqual(availability(conn, "nemotron-3-nano"), 0)

    def test_a_model_the_sweep_just_wrote_is_left_alone(self):
        conn = db_with([("nvidia", "minimax-m3", 1, "text", RUN_START + 30)])
        mhc.retire_unseen_models(conn, "nvidia", RUN_START)
        self.assertEqual(availability(conn, "minimax-m3"), 1)

    def test_a_model_that_passes_the_next_sweep_comes_back(self):
        """The row is kept, not deleted, so the next INSERT OR REPLACE revives it."""
        conn = db_with([("nvidia", "nemotron-3-nano", 1, "text", RUN_START - 86400)])
        mhc.retire_unseen_models(conn, "nvidia", RUN_START)
        conn.execute("INSERT OR REPLACE INTO model_health "
                     "(provider, model_id, latency_ms, available, supports_tools, category, last_check) "
                     "VALUES ('nvidia', 'nemotron-3-nano', 10, 1, 1, 'text', ?)", (RUN_START + 600,))
        mhc.retire_unseen_models(conn, "nvidia", RUN_START + 600)
        self.assertEqual(availability(conn, "nemotron-3-nano"), 1)

    def test_another_providers_models_are_not_touched(self):
        conn = db_with([("groq", "whisper-large", 1, "text", RUN_START - 86400)])
        mhc.retire_unseen_models(conn, "nvidia", RUN_START)
        self.assertEqual(availability(conn, "whisper-large"), 1)

    def test_audio_and_video_rows_belong_to_other_probes_and_survive(self):
        """model-audio-check and model-media-check run on their own schedules."""
        conn = db_with([("nvidia", "riva-tts", 1, "audio", RUN_START - 86400),
                        ("nvidia", "video-detector", 1, "video", RUN_START - 86400)])
        mhc.retire_unseen_models(conn, "nvidia", RUN_START)
        self.assertEqual(availability(conn, "riva-tts"), 1)
        self.assertEqual(availability(conn, "video-detector"), 1)


if __name__ == "__main__":
    unittest.main()
