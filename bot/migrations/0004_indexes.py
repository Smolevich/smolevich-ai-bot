from yoyo import step

__depends__ = {"0003_request_log_and_tools"}

steps = [
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_health_log_ts ON model_health_log (ts)",
        "DROP INDEX IF EXISTS idx_model_health_log_ts",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_health_log_provider_model ON model_health_log (provider, model_id)",
        "DROP INDEX IF EXISTS idx_model_health_log_provider_model",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts)",
        "DROP INDEX IF EXISTS idx_request_log_ts",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_request_log_uid ON request_log (uid)",
        "DROP INDEX IF EXISTS idx_request_log_uid",
    ),
]
