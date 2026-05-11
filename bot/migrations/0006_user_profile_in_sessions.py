from yoyo import step

__depends__ = {"0005_health_log_and_capabilities"}

steps = [
    step(
        "ALTER TABLE sessions ADD COLUMN profile TEXT DEFAULT 'beginner'",
        ignore_errors="apply",
    ),
]
