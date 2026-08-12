from yoyo import step

__depends__ = {"0007_model_benchmark_jobs"}

# HuggingFace was retired once its free tier ended. Its 162 rows in model_health
# stay `available=0` forever and make the provider look merely broken instead of gone.
# model_health_log keeps the history — that data is the point of the whole probe.
steps = [
    step(
        "DELETE FROM model_health WHERE provider = 'huggingface'",
        ignore_errors="apply",
    ),
]
