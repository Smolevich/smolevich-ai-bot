from yoyo import step

__depends__ = {"0006_user_profile_in_sessions"}

steps = [
    step(
        """CREATE TABLE IF NOT EXISTS model_benchmark_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            task_id TEXT NOT NULL,
            sample_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            priority INTEGER DEFAULT 100,
            attempts INTEGER DEFAULT 0,
            locked_by TEXT,
            locked_ts INTEGER,
            finished_ts INTEGER,
            error TEXT
        )""",
        "DROP TABLE IF EXISTS model_benchmark_jobs",
    ),
    step(
        """CREATE TABLE IF NOT EXISTS model_benchmark_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            batch_id TEXT,
            ts INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            task_id TEXT NOT NULL,
            sample_id TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            error TEXT,
            response_excerpt TEXT,
            details_json TEXT
        )""",
        "DROP TABLE IF EXISTS model_benchmark_results",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_benchmark_jobs_status "
        "ON model_benchmark_jobs (status, priority, id)",
        "DROP INDEX IF EXISTS idx_model_benchmark_jobs_status",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_benchmark_jobs_batch "
        "ON model_benchmark_jobs (batch_id)",
        "DROP INDEX IF EXISTS idx_model_benchmark_jobs_batch",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_benchmark_jobs_dedup "
        "ON model_benchmark_jobs (provider, model_id, mode, task_id, sample_id, status)",
        "DROP INDEX IF EXISTS idx_model_benchmark_jobs_dedup",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_benchmark_results_ts "
        "ON model_benchmark_results (ts)",
        "DROP INDEX IF EXISTS idx_model_benchmark_results_ts",
    ),
    step(
        "CREATE INDEX IF NOT EXISTS idx_model_benchmark_results_pmm "
        "ON model_benchmark_results (provider, model_id, mode, task_id, sample_id, ts)",
        "DROP INDEX IF EXISTS idx_model_benchmark_results_pmm",
    ),
]
