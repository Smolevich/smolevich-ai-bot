"""JSON-serialisable contracts. Stdlib only — this module is imported inside the workflow sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "smolevich-bench"
CLAUDE_TASK_QUEUE = "smolevich-bench-claude"
TERMINAL_ERROR_TYPE = "BenchmarkTerminalError"

# Re-measured from the providers' own `x-ratelimit-*` headers on 2026-09-08 (the numbers
# from 2026-08-17 had moved: groq said 6000 tokens/min then, it says 8000 now):
#   groq      allam-2-7b 7000 req/min, 6000 tok/min; gpt-oss-20b/120b and qwen3.6/3.8-27b
#             1000 req/min, 8000 tok/min; compound-mini 250 req/day, 70000 tok.
#   nvidia / openrouter: no 429 at all across 480 samples, so they keep three at a time.
# Hence one at a time for groq — parallelism cannot buy throughput that the per-minute
# ceiling does not allow, it only converts it into rejections.
PROVIDER_CONCURRENCY = {"groq": 1, "openrouter": 3, "nvidia": 3}
DEFAULT_CONCURRENCY = 1

# Concurrency alone did not help: groq limits requests per minute, so 20 samples fired back
# to back still 429 (80 of 125 groq calls on 2026-08-13). Pause between chunks to stay under
# the per-minute ceiling. Five and nine seconds produced 22% and 35% rejections over two days.
#
# 16s is kept even though the token ceiling rose to 8000/min: on qwen3.8-27b the binding
# limit is not the one in the headers but the output-token ceiling, which groq reveals only
# in the body of a 429 (OTPM: Limit 1000). Per-request budgets against it live in
# agent/rate_limits.py, which the benchmark applies to max_tokens.
PROVIDER_PAUSE_SEC = {"groq": 16.0, "openrouter": 1.0, "nvidia": 0.5}
DEFAULT_PAUSE_SEC = 2.0


@dataclass
class BatchInput:
    batch_id: str = ""
    publish: bool = True


@dataclass
class JobRef:
    id: int = 0
    provider: str = ""
    mode: str = "native"
    model_id: str = ""


@dataclass
class LaneInput:
    provider: str = ""
    refs: list[JobRef] = field(default_factory=list)


@dataclass
class JobOutcome:
    job_id: int = 0
    ok: bool = False
    score: float = 0.0
    latency_ms: int = 0
    error: str = ""
