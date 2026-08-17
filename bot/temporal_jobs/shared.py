"""JSON-serialisable contracts. Stdlib only — this module is imported inside the workflow sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "smolevich-bench"
CLAUDE_TASK_QUEUE = "smolevich-bench-claude"
TERMINAL_ERROR_TYPE = "BenchmarkTerminalError"

# Measured from the providers' own headers on 2026-08-17:
#   groq      x-ratelimit-limit-tokens: 6000 per minute — at ~1500 tokens an answer that is
#             four requests a minute, no matter how few connections we open;
#   cerebras  x-ratelimit-remaining-requests-minute: 4 (tokens are not the binding limit);
#   nvidia / openrouter: no 429 at all across 480 samples, so they keep three at a time.
# Hence one at a time for the metered pair — parallelism cannot buy throughput that the
# per-minute ceiling does not allow, it only converts it into rejections.
PROVIDER_CONCURRENCY = {"groq": 1, "openrouter": 3, "cerebras": 1, "nvidia": 3}
DEFAULT_CONCURRENCY = 1

# Concurrency alone did not help: groq and cerebras limit requests per minute, so 20 samples
# fired back to back still 429 (80 of 125 groq calls, 40 of 62 cerebras ones on 2026-08-13).
# Pause between chunks to stay under the per-minute ceiling.
# Spacing that fits the ceilings above: 6000 tokens/min ÷ ~1500 per answer ≈ 16s for groq,
# 4 requests/min ≈ 15s for cerebras. Five and nine seconds still produced 22% and 35%
# rejections over two days.
PROVIDER_PAUSE_SEC = {"groq": 16.0, "cerebras": 15.0, "openrouter": 1.0, "nvidia": 0.5}
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
