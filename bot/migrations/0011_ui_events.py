from yoyo import step

__depends__ = {"0010_provider_state"}

# What people actually press. request_log only sees messages that reached a model, so
# every button, every menu screen and every abandoned step was invisible — including the
# case where someone opens the board and leaves without trying anything.
# route = which screen ("menu:top", "try", "quick:chat"), arg = its tail, kept apart so
# route does not explode into hundreds of values.
steps = [
    step(
        """CREATE TABLE IF NOT EXISTS ui_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            route TEXT NOT NULL,
            arg TEXT
        )"""
    ),
    step("CREATE INDEX IF NOT EXISTS idx_ui_events_ts ON ui_events(ts)"),
    step("CREATE INDEX IF NOT EXISTS idx_ui_events_uid ON ui_events(uid, ts)"),
]
