from yoyo import step

__depends__ = {"0002_provider_and_health"}

steps = [
    step(
        "ALTER TABLE sessions ADD COLUMN tools_enabled INTEGER DEFAULT 1",
        ignore_errors="apply",
    ),
    step(
        """CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, uid INTEGER, provider TEXT, model TEXT,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            finish_reason TEXT, tool_calls INTEGER, error TEXT)""",
        "DROP TABLE request_log",
    ),
]
