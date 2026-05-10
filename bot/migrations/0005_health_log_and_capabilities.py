from yoyo import step

__depends__ = {"0004_indexes"}

steps = [
    step(
        """CREATE TABLE IF NOT EXISTS model_health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, provider TEXT, model_id TEXT,
            latency_ms INTEGER, available INTEGER, error TEXT
        )""",
        "DROP TABLE IF EXISTS model_health_log",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_health_log_ts ON model_health_log (ts)",
        "DROP INDEX IF EXISTS idx_model_health_log_ts",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_health_log_provider_model ON model_health_log (provider, model_id)",
        "DROP INDEX IF EXISTS idx_model_health_log_provider_model",
    ),
    step(
        "ALTER TABLE model_health ADD COLUMN capabilities TEXT DEFAULT ''",
        ignore_errors="apply",
    ),
]
