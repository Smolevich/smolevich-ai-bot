from yoyo import step

__depends__ = {"0001_initial"}

steps = [
    step(
        "ALTER TABLE sessions ADD COLUMN provider TEXT DEFAULT 'openrouter'",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE users ADD COLUMN prompt_tokens INTEGER DEFAULT 0",
        ignore_errors="apply",
    ),
    step(
        "ALTER TABLE users ADD COLUMN completion_tokens INTEGER DEFAULT 0",
        ignore_errors="apply",
    ),
    step(
        """CREATE TABLE model_health (
            provider TEXT,
            model_id TEXT,
            latency_ms INTEGER,
            available INTEGER,
            supports_tools INTEGER,
            category TEXT,
            last_check INTEGER,
            PRIMARY KEY (provider, model_id)
        )""",
        "DROP TABLE model_health"
    ),
]
