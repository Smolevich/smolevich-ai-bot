"""JSON-serialisable contracts. Stdlib only — this module is imported inside the workflow sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "smolevich-bench"
CLAUDE_TASK_QUEUE = "smolevich-bench-claude"
TERMINAL_ERROR_TYPE = "BenchmarkTerminalError"

# groq answers 429 from the third parallel request on a free model; the others tolerate more.
PROVIDER_CONCURRENCY = {"groq": 2, "openrouter": 3, "cerebras": 3, "nvidia": 3}
DEFAULT_CONCURRENCY = 2

# Concurrency alone did not help: groq and cerebras limit requests per minute, so 20 samples
# fired back to back still 429 (80 of 125 groq calls, 40 of 62 cerebras ones on 2026-08-13).
# Pause between chunks to stay under the per-minute ceiling.
PROVIDER_PAUSE_SEC = {"groq": 5.0, "cerebras": 5.0, "openrouter": 1.0, "nvidia": 0.5}
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
