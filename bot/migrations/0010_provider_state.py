from yoyo import step

__depends__ = {"0009_benchmark_results_unique_job"}

# HuggingFace answered 402 on all ~130 models every 10 minutes for three months and nobody
# noticed. This row is what lets the health check park a provider that shut its door.
steps = [
    step(
        """CREATE TABLE IF NOT EXISTS provider_state (
            provider TEXT PRIMARY KEY,
            disabled_until INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            consecutive_dead_runs INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT 0
        )""",
        "DROP TABLE IF EXISTS provider_state",
    ),
]
