from yoyo import step

__depends__ = {"0012_drop_cerebras_rows"}

# Two things the bot could not tell apart before.
#
# `model_pinned` — whether the model in a session row is the person's own choice or just
# whatever the bot picked last time. Without it there was no way to keep a chosen model
# and at the same time move everyone else onto the current leader of the measurement,
# so a delisted model stayed in the row until the human went and changed it by hand.
#
# `voice_usage` — one row per transcription or voicing, so an hourly ceiling can exist.
# Text has no ceiling of ours: the free tiers and the breaker are the ceiling.
steps = [
    step("ALTER TABLE sessions ADD COLUMN model_pinned INTEGER NOT NULL DEFAULT 0"),
    step(
        """CREATE TABLE IF NOT EXISTS voice_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            kind TEXT NOT NULL
        )"""
    ),
    step("CREATE INDEX IF NOT EXISTS idx_voice_usage_uid ON voice_usage(uid, kind, ts)"),
]
