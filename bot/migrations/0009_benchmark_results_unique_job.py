from yoyo import step

__depends__ = {"0008_drop_retired_provider_rows"}

# A result row is the current state of a job, not a log of attempts. Without this,
# a sample that failed on 429 and passed on retry stored both 0.0 and 1.0, and the
# model was scored 0.5 for an answer it got right. 380 of 3 487 rows were such pairs.
steps = [
    step(
        """DELETE FROM model_benchmark_results
           WHERE job_id IS NOT NULL
             AND rowid NOT IN (SELECT MAX(rowid) FROM model_benchmark_results
                               WHERE job_id IS NOT NULL GROUP BY job_id)"""
    ),
    step(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_bench_results_job
           ON model_benchmark_results(job_id) WHERE job_id IS NOT NULL"""
    ),
]
