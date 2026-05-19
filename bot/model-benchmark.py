#!/usr/bin/env python3
"""Queue-based benchmark for free-tier text models.

Pipeline (cron driven, once per day):
    enqueue → work → leaderboard --publish → leaderboard --publish-tasks → purge

Native tasks run as plain HTTP completions in parallel. Claude tasks run in a
podman sandbox via acpx; only one acpx container is allowed on the host at a
time (global flock shared with vds-agent.py). Workspaces are removed after
scoring unless the run failed and BOT_BENCHMARK_KEEP_FAILED=1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from agent.acpx_lock import acpx_lock, active_recent
from agent.benchmark_scoring import score as score_response
from agent.config import DB_FILE, PROVIDERS, PROXY_URL, SESSIONS_ROOT
from agent.provider_api import load_provider_key, make_opener

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

DEFAULT_MODELS_PER_PROVIDER = 3
DEFAULT_LOOKBACK_HOURS = 168
STABLE_MIN_SUCCESS_RATE = 0.9
STABLE_MIN_CHECKS = 6
STABLE_MAX_LATENCY_MS = 8000

DEFAULT_NATIVE_WORKERS = 4
DEFAULT_MAX_JOBS = 200
DEFAULT_TIMEOUT = 60
DEFAULT_CLAUDE_TIMEOUT = 120
DEFAULT_LOOKBACK_BENCH_HOURS = 168
MAX_RESPONSE_EXCERPT = 4000

ACTIVE_SKIP_WINDOW_SEC = 120
LEADERBOARD_ENDPOINT = "https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/free-models"
TASKS_ENDPOINT = "https://notes-share.smolevich90.workers.dev/api/smolevich-ai-bot/benchmark-tasks"

PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "cerebras": "Cerebras",
    "nvidia": "NVIDIA",
    "huggingface": "Hugging Face",
}

DEFAULT_TASKS_PATH = os.environ.get(
    "BOT_BENCHMARK_TASKS",
    str(Path(__file__).resolve().parent / "benchmark-tasks.json"),
)
DEFAULT_METHODOLOGY_PATH = os.environ.get(
    "BOT_BENCHMARK_METHODOLOGY",
    str(Path(__file__).resolve().parent / "benchmark-tasks.md"),
)
DEFAULT_DATASETS_DIR = os.environ.get(
    "BOT_BENCHMARK_DATASETS",
    str(Path(__file__).resolve().parent / "benchmark-datasets"),
)
DEFAULT_BENCHMARK_ROOT = os.environ.get(
    "BOT_BENCHMARK_ROOT",
    str(Path(SESSIONS_ROOT) / "benchmarks"),
)


def now_ts() -> int:
    return int(time.time())


def kill_switch() -> bool:
    return os.environ.get("BOT_BENCHMARK_DISABLED", "").strip() == "1"


# ---------------------------------------------------------------------------
# Loading tasks/datasets
# ---------------------------------------------------------------------------


def load_tasks(path: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("Tasks file not found: %s", path)
        return {"native": [], "claude": []}


def load_dataset(datasets_dir: str, filename: str) -> list[dict[str, Any]]:
    path = Path(datasets_dir) / filename
    if not path.exists():
        log.error("Dataset file not found: %s", path)
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("samples") or []


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def stable_models(conn: sqlite3.Connection, provider: str, limit: int, lookback_hours: int) -> list[dict[str, Any]]:
    cutoff = now_ts() - max(1, lookback_hours) * 3600
    try:
        rows = conn.execute(
            """
            WITH recent AS (
                SELECT provider, model_id,
                       COUNT(*) AS checks,
                       SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS ok_checks,
                       AVG(CASE WHEN available = 1 AND latency_ms > 0 THEN latency_ms END) AS avg_latency_ms,
                       MAX(ts) AS last_seen
                FROM model_health_log
                WHERE ts >= ?
                GROUP BY provider, model_id
            )
            SELECT mh.provider, mh.model_id, mh.latency_ms,
                   COALESCE(recent.checks, 0),
                   COALESCE(recent.ok_checks, 0),
                   COALESCE(CAST(recent.ok_checks AS REAL) / NULLIF(recent.checks, 0), 0.0) AS success_rate,
                   COALESCE(recent.avg_latency_ms, NULLIF(mh.latency_ms, 0), 999999) AS stable_latency_ms,
                   COALESCE(recent.last_seen, mh.last_check, 0)
            FROM model_health mh
            LEFT JOIN recent ON recent.provider = mh.provider AND recent.model_id = mh.model_id
            WHERE mh.provider = ?
              AND mh.category = 'text'
              AND mh.available = 1
              AND COALESCE(recent.checks, 0) >= ?
              AND COALESCE(CAST(recent.ok_checks AS REAL) / NULLIF(recent.checks, 0), 0.0) >= ?
              AND COALESCE(recent.avg_latency_ms, NULLIF(mh.latency_ms, 0), 999999) <= ?
            ORDER BY success_rate DESC, stable_latency_ms ASC, checks DESC, mh.model_id ASC
            LIMIT ?
            """,
            (
                cutoff,
                provider,
                STABLE_MIN_CHECKS,
                STABLE_MIN_SUCCESS_RATE,
                STABLE_MAX_LATENCY_MS,
                max(1, limit),
            ),
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise
    return [
        {
            "provider": row[0],
            "model_id": row[1],
            "latency_ms": int(row[2] or 0),
            "checks": int(row[3] or 0),
            "ok_checks": int(row[4] or 0),
            "success_rate": float(row[5] or 0.0),
            "stable_latency_ms": int(row[6] or 0),
            "last_seen": int(row[7] or 0),
        }
        for row in rows
    ]


def has_pending_duplicate(conn: sqlite3.Connection, provider: str, model_id: str, mode: str, task_id: str, sample_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM model_benchmark_jobs
        WHERE provider = ? AND model_id = ? AND mode = ? AND task_id = ? AND sample_id = ?
              AND status IN ('queued', 'running')
        LIMIT 1
        """,
        (provider, model_id, mode, task_id, sample_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def pick_samples(samples: list[dict[str, Any]], count: int, seed_key: str) -> list[dict[str, Any]]:
    if not samples:
        return []
    rng = random.Random(int(hashlib.sha256(seed_key.encode("utf-8")).hexdigest(), 16))
    pool = list(samples)
    rng.shuffle(pool)
    return pool[: max(1, count)]


def enqueue_jobs(args: argparse.Namespace) -> str:
    providers = list(PROVIDERS.keys()) if args.provider == "all" else [args.provider]
    batch_id = args.batch_id or time.strftime("%Y%m%d-%H%M%S")
    tasks = load_tasks(args.tasks_path)
    native_tasks = tasks.get("native") or []
    claude_tasks = tasks.get("claude") or []
    created = 0
    skipped = 0
    with connect(args.db) as conn:
        for provider in providers:
            if not load_provider_key(provider):
                log.info("Skipping %s: missing API key", provider)
                continue
            models = stable_models(conn, provider, args.models_per_provider, args.lookback_hours)
            log.info("Selected %s stable models for %s", len(models), provider)
            for rank, model in enumerate(models):
                # native — для всех топ-3 моделей; claude — только топ-1.
                groups: list[tuple[str, list[dict[str, Any]]]] = [("native", native_tasks)]
                if rank == 0:
                    groups.append(("claude", claude_tasks))
                if args.mode != "all":
                    groups = [(m, t) for m, t in groups if m == args.mode]
                for mode, task_list in groups:
                    for task in task_list:
                        samples = load_dataset(args.datasets_dir, task["dataset"])
                        seed_key = f"{batch_id}:{provider}:{model['model_id']}:{task['id']}"
                        chosen = pick_samples(samples, int(task.get("samples_per_run") or 1), seed_key)
                        for sample in chosen:
                            sample_id = str(sample.get("id") or "")
                            if has_pending_duplicate(conn, provider, model["model_id"], mode, task["id"], sample_id):
                                skipped += 1
                                continue
                            payload = {"task": task, "sample": sample, "selection": model}
                            ts = now_ts()
                            conn.execute(
                                """
                                INSERT INTO model_benchmark_jobs
                                    (batch_id, created_ts, updated_ts, status, provider, model_id,
                                     mode, task_id, sample_id, payload_json, priority)
                                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    batch_id, ts, ts,
                                    provider, model["model_id"], mode,
                                    task["id"], sample_id,
                                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                    10 if mode == "native" else 20,
                                ),
                            )
                            created += 1
        conn.commit()
    log.info("Enqueued batch=%s created=%s skipped_pending=%s", batch_id, created, skipped)
    return batch_id


# ---------------------------------------------------------------------------
# Claim / release
# ---------------------------------------------------------------------------


def reset_stale_jobs(conn: sqlite3.Connection, stale_after_sec: int) -> int:
    cutoff = now_ts() - max(60, stale_after_sec)
    cur = conn.execute(
        """
        UPDATE model_benchmark_jobs
        SET status = 'queued', updated_ts = ?, locked_by = NULL, locked_ts = NULL,
            error = 'reset stale running job'
        WHERE status = 'running' AND COALESCE(locked_ts, 0) < ?
        """,
        (now_ts(), cutoff),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def claim_jobs(args: argparse.Namespace, mode: str, limit: int, worker_id: str) -> list[dict[str, Any]]:
    with connect(args.db) as conn:
        reset_count = reset_stale_jobs(conn, args.stale_after)
        if reset_count:
            log.info("Reset %s stale jobs", reset_count)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, batch_id, provider, model_id, mode, task_id, sample_id, payload_json, attempts
            FROM model_benchmark_jobs
            WHERE status = 'queued' AND mode = ? AND attempts < ?
            ORDER BY priority ASC, id ASC
            LIMIT ?
            """,
            (mode, args.max_attempts, max(1, limit)),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE model_benchmark_jobs
                SET status = 'running', updated_ts = ?, locked_by = ?, locked_ts = ?,
                    attempts = attempts + 1
                WHERE id IN ({placeholders})
                """,
                (now_ts(), worker_id, now_ts(), *ids),
            )
        conn.commit()
    jobs = []
    for row in rows:
        try:
            payload = json.loads(row[7])
        except Exception:
            payload = {}
        jobs.append(
            {
                "id": row[0],
                "batch_id": row[1],
                "provider": row[2],
                "model_id": row[3],
                "mode": row[4],
                "task_id": row[5],
                "sample_id": row[6],
                "task": payload.get("task"),
                "sample": payload.get("sample"),
                "attempts": int(row[8] or 0) + 1,
            }
        )
    return [j for j in jobs if j.get("task") and j.get("sample")]


def release_claimed_jobs(args: argparse.Namespace, jobs: list[dict[str, Any]], reason: str) -> None:
    if not jobs:
        return
    ids = [j["id"] for j in jobs]
    placeholders = ",".join("?" for _ in ids)
    with connect(args.db) as conn:
        conn.execute(
            f"""
            UPDATE model_benchmark_jobs
            SET status = 'queued', updated_ts = ?,
                locked_by = NULL, locked_ts = NULL,
                attempts = MAX(attempts - 1, 0), error = ?
            WHERE id IN ({placeholders})
            """,
            (now_ts(), reason[:500], *ids),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Native / claude completion
# ---------------------------------------------------------------------------


def build_prompt(task: dict[str, Any], sample: dict[str, Any]) -> str:
    question = (sample.get("question") or "").strip()
    suffix = (task.get("prompt_suffix") or "").strip()
    if suffix:
        return f"{question}\n\n{suffix}"
    return question


def _extract_usage(raw: dict[str, Any]) -> dict[str, int]:
    usage = raw.get("usage") or {}
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            out[key] = int(value)
    return out


def native_completion(
    provider: str, model_id: str, task: dict[str, Any], sample: dict[str, Any], timeout: int
) -> tuple[str, int, str | None, dict[str, int]]:
    prov = PROVIDERS[provider]
    api_key = load_provider_key(provider)
    if not api_key:
        return "", 0, "missing_api_key", {}
    prompt = build_prompt(task, sample)
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "Ты проходишь короткий benchmark. Следуй инструкциям формата ответа строго."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": int(task.get("max_tokens", 256)),
    }
    opener = make_opener(prov.get("proxy", False))
    started = time.time()
    req = urllib.request.Request(
        prov["url"],
        json.dumps(payload).encode("utf-8"),
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        latency_ms = int((time.time() - started) * 1000)
        choices = raw.get("choices") or []
        usage = _extract_usage(raw)
        if not choices:
            return "", latency_ms, "empty_choices", usage
        message = (choices[0].get("message") or {})
        content = message.get("content")
        if content is None:
            content = choices[0].get("text", "")
        return str(content or ""), latency_ms, None, usage
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return "", int((time.time() - started) * 1000), f"HTTP {e.code}: {e.reason} {body}".strip(), {}
    except Exception as e:
        return "", int((time.time() - started) * 1000), str(e)[:500], {}


def provider_base_url(provider: str) -> str:
    return PROVIDERS[provider]["url"].rsplit("/chat/completions", 1)[0]


def claude_completion(
    provider: str,
    model_id: str,
    task: dict[str, Any],
    sample: dict[str, Any],
    timeout: int,
    benchmark_root: Path,
) -> tuple[str, int, str | None, Path]:
    api_key = load_provider_key(provider)
    if not api_key:
        return "", 0, "missing_api_key", benchmark_root
    if not shutil.which("podman"):
        return "", 0, "podman_not_found", benchmark_root

    run_id = f"{now_ts()}_{provider}_{uuid.uuid4().hex[:8]}"
    workspace = benchmark_root / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(workspace, 0o777)
    except Exception:
        pass
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(exist_ok=True)
    try:
        os.chmod(claude_dir, 0o777)
        (claude_dir / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}),
            encoding="utf-8",
        )
    except Exception:
        pass

    base_url = provider_base_url(provider)
    anthropic_base = base_url.replace("/v1", "")
    append_prompt = (
        "Benchmark mode. Use at most 3 tool calls total. If a tool fails twice, "
        "stop and finalize. Do not re-read files you just wrote."
    )
    prompt = build_prompt(task, sample)
    cmd = [
        "podman", "run", "--rm",
        "--network=host",
        "--user", "1000:1000",
        "--memory=1g", "--memory-swap=1g",
        "--cpus=1", "--pids-limit=256",
        "-e", f"OPENAI_API_KEY={api_key}",
        "-e", f"ANTHROPIC_BASE_URL={anthropic_base}",
        "-e", f"ANTHROPIC_AUTH_TOKEN={api_key}",
        "-e", f"ANTHROPIC_API_KEY={api_key}",
        "-e", f"OPENAI_BASE_URL={base_url}",
        "-e", f"OPENAI_MODEL={model_id}",
        "-e", f"ANTHROPIC_DEFAULT_OPUS_MODEL={model_id}",
        "-e", f"ANTHROPIC_DEFAULT_SONNET_MODEL={model_id}",
        "-e", f"ANTHROPIC_DEFAULT_HAIKU_MODEL={model_id}",
        "-e", f"CLAUDE_CODE_SUBAGENT_MODEL={model_id}",
        "-e", "HOME=/workspace/.claude-home",
        "-e", "XDG_CONFIG_HOME=/workspace/.claude-config",
        "-e", "XDG_CACHE_HOME=/workspace/.claude-cache",
        "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",
        "-e", "IS_SANDBOX=1",
        "-e", f"ACPX_APPEND_SYSTEM_PROMPT={append_prompt}",
    ]
    if PROVIDERS[provider].get("proxy", False) and PROXY_URL:
        cmd += ["-e", f"HTTPS_PROXY={PROXY_URL}", "-e", f"HTTP_PROXY={PROXY_URL}", "-e", f"ALL_PROXY={PROXY_URL}"]
    cmd += [
        "-v", f"{workspace}:/workspace",
        "-w", "/workspace",
        "localhost/acpx-claude:latest",
        "acpx", "--cwd", "/workspace", "--format", "text",
        "--approve-all", "--non-interactive-permissions", "deny",
        "--timeout", str(timeout),
        "claude", "exec", prompt,
    ]

    started = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        return "", int((time.time() - started) * 1000), "wall_clock_timeout", workspace
    except Exception as e:
        return "", int((time.time() - started) * 1000), str(e)[:500], workspace

    latency_ms = int((time.time() - started) * 1000)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return stdout, latency_ms, f"exit={result.returncode}: {stderr[:500]}", workspace
    return stdout, latency_ms, None, workspace


# ---------------------------------------------------------------------------
# Run / score / persist
# ---------------------------------------------------------------------------


def complete_job(args: argparse.Namespace, job: dict[str, Any], result: dict[str, Any]) -> None:
    final_status = "done" if result.get("ok") else "failed"
    if result.get("error") and int(job.get("attempts") or 0) < int(args.max_attempts):
        final_status = "queued"
    with connect(args.db) as conn:
        cur = conn.execute(
            """
            INSERT INTO model_benchmark_results
                (job_id, batch_id, ts, provider, model_id, mode, task_id, sample_id,
                 latency_ms, ok, score, error, response_excerpt, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"], job["batch_id"], now_ts(),
                job["provider"], job["model_id"], job["mode"],
                job["task_id"], job["sample_id"],
                int(result.get("latency_ms") or 0),
                1 if result.get("ok") else 0,
                float(result.get("score") or 0.0),
                (result.get("error") or "")[:500] if result.get("error") else None,
                (result.get("response") or "")[:MAX_RESPONSE_EXCERPT],
                json.dumps(result.get("details") or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            """
            UPDATE model_benchmark_jobs
            SET status = ?, updated_ts = ?, finished_ts = ?, error = ?,
                locked_by = NULL, locked_ts = NULL
            WHERE id = ?
            """,
            (
                final_status,
                now_ts(),
                now_ts() if final_status in ("done", "failed") else None,
                (result.get("error") or "")[:500] if result.get("error") else None,
                job["id"],
            ),
        )
        conn.commit()
        result["result_id"] = cur.lastrowid


def run_job(args: argparse.Namespace, job: dict[str, Any]) -> dict[str, Any]:
    provider = job["provider"]
    model_id = job["model_id"]
    task = job["task"]
    sample = job["sample"]
    mode = job["mode"]
    workspace: Path | None = None
    usage: dict[str, int] = {}
    if mode == "native":
        response, latency_ms, error, usage = native_completion(provider, model_id, task, sample, args.timeout)
    else:
        response, latency_ms, error, workspace = claude_completion(
            provider, model_id, task, sample, args.claude_timeout, Path(args.benchmark_root),
        )
    ground_truth = sample.get("ground_truth")
    ok, sc, detail = score_response(task["kind"], response, ground_truth, workspace)
    if error:
        ok = False
        sc = 0.0
    result = {
        "provider": provider,
        "model_id": model_id,
        "mode": mode,
        "task_id": task["id"],
        "sample_id": sample.get("id", ""),
        "latency_ms": latency_ms,
        "ok": ok,
        "score": sc,
        "error": error,
        "response": response,
        "details": {
            "detail": detail,
            "workspace": str(workspace) if workspace else "",
            "ground_truth": ground_truth,
            "usage": usage,
        },
    }
    if workspace is not None:
        keep = bool(os.environ.get("BOT_BENCHMARK_KEEP_FAILED", "").strip()) and not ok
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)
    complete_job(args, job, result)
    return result


def log_job_result(result: dict[str, Any]) -> None:
    status = "ok" if result["ok"] else "fail"
    err = f" error={result['error']}" if result.get("error") else ""
    log.info(
        "%s %s/%s mode=%s task=%s sample=%s latency=%sms score=%.2f%s",
        status, result["provider"], result["model_id"], result["mode"],
        result["task_id"], result.get("sample_id", ""), result["latency_ms"], result["score"], err,
    )


def run_claimed(args: argparse.Namespace, jobs: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    log.info("Running %s jobs, workers=%s", len(jobs), workers)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_job, args, j): j for j in jobs}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                job = futures[future]
                result = {
                    "provider": job["provider"], "model_id": job["model_id"],
                    "mode": job["mode"], "task_id": job["task_id"], "sample_id": job["sample_id"],
                    "latency_ms": 0, "ok": False, "score": 0.0, "error": str(e)[:500],
                }
                complete_job(args, job, result)
            log_job_result(result)
            results.append(result)
    return results


def work_mode(args: argparse.Namespace, mode: str, limit: int, worker_id: str) -> list[dict[str, Any]]:
    if mode == "claude" and active_recent(ACTIVE_SKIP_WINDOW_SEC):
        log.info("vds-agent recently active, deferring claude tick")
        return []
    jobs = claim_jobs(args, mode, limit, worker_id)
    if not jobs:
        log.info("No queued %s jobs", mode)
        return []
    if mode == "claude":
        with acpx_lock(timeout=0, holder=f"benchmark:{worker_id}") as got:
            if not got:
                release_claimed_jobs(args, jobs, "acpx_lock_busy")
                log.info("acpx lock busy, deferring %s claude jobs", len(jobs))
                return []
            return run_claimed(args, jobs, 1)
    return run_claimed(args, jobs, args.native_workers)


def work_jobs(args: argparse.Namespace) -> list[dict[str, Any]]:
    if kill_switch():
        log.info("BOT_BENCHMARK_DISABLED=1, skipping work")
        return []
    worker_id = args.worker_id or f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    modes = ["native", "claude"] if args.mode == "all" else [args.mode]
    results: list[dict[str, Any]] = []
    for mode in modes:
        if len(results) >= args.max_jobs:
            break
        left = args.max_jobs - len(results)
        results.extend(work_mode(args, mode, left, worker_id))
    return results


def print_summary(results: list[dict[str, Any]]) -> None:
    if not results:
        return
    total = len(results)
    ok_count = sum(1 for r in results if r["ok"])
    log.info("Benchmark worker summary: %s/%s jobs passed", ok_count, total)


# ---------------------------------------------------------------------------
# Leaderboard / tasks payload
# ---------------------------------------------------------------------------


def infer_strengths(model_id: str, native_score: float, claude_score: float, latency_ms: int) -> list[str]:
    mid = model_id.lower()
    strengths: list[str] = []
    if native_score >= 0.8:
        strengths.append("Stable chat")
    if claude_score >= 0.7:
        strengths.append("Agent mode")
    if latency_ms and latency_ms < 2500:
        strengths.append("Fast")
    if any(k in mid for k in ("coder", "code", "qwen", "deepseek", "devstral", "codestral")):
        strengths.append("Code")
    if any(k in mid for k in ("70b", "120b", "405b", "large", "nemotron", "reason")):
        strengths.append("Reasoning")
    if len(strengths) < 3:
        strengths.append("Russian/English")
    return strengths[:5]


def context_window_hint(model_id: str) -> str:
    mid = model_id.lower()
    for pattern, label in [
        ("1m", "1M"), ("256k", "256K"), ("200k", "200K"),
        ("131k", "128K"), ("128k", "128K"),
        ("64k", "64K"), ("32k", "32K"), ("16k", "16K"), ("8k", "8K"),
    ]:
        if pattern in mid:
            return label
    return ""


def leaderboard_payload(args: argparse.Namespace) -> dict[str, Any]:
    cutoff = now_ts() - max(1, args.lookback_hours) * 3600
    tasks = load_tasks(args.tasks_path)
    with connect(args.db) as conn:
        try:
            agg_rows = conn.execute(
                """
                SELECT provider, model_id, mode,
                       COUNT(*) AS runs,
                       SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_runs,
                       AVG(score) AS avg_score,
                       AVG(CASE WHEN ok = 1 AND latency_ms > 0 THEN latency_ms END) AS avg_latency_ms,
                       MAX(ts) AS last_bench
                FROM model_benchmark_results
                WHERE ts >= ?
                GROUP BY provider, model_id, mode
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                agg_rows = []
            else:
                raise
        try:
            recent_rows = conn.execute(
                """
                SELECT provider, model_id, mode, task_id, sample_id, latency_ms, ok, score, ts,
                       json_extract(details_json, '$.usage.prompt_tokens'),
                       json_extract(details_json, '$.usage.completion_tokens'),
                       json_extract(details_json, '$.usage.total_tokens')
                FROM model_benchmark_results
                WHERE ts >= ?
                ORDER BY ts DESC
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            recent_rows = []
        try:
            health_rows = conn.execute(
                """
                SELECT mh.provider, mh.model_id, mh.available, mh.latency_ms,
                       COALESCE(h.checks, 0),
                       COALESCE(h.ok_checks, 0),
                       COALESCE(CAST(h.ok_checks AS REAL) / NULLIF(h.checks, 0), 0.0) AS rate,
                       COALESCE(h.avg_latency_ms, NULLIF(mh.latency_ms, 0), 999999) AS latency
                FROM model_health mh
                LEFT JOIN (
                    SELECT provider, model_id,
                           COUNT(*) AS checks,
                           SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS ok_checks,
                           AVG(CASE WHEN available = 1 AND latency_ms > 0 THEN latency_ms END) AS avg_latency_ms
                    FROM model_health_log
                    WHERE ts >= ?
                    GROUP BY provider, model_id
                ) h ON h.provider = mh.provider AND h.model_id = mh.model_id
                WHERE mh.category = 'text' AND mh.available = 1
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            health_rows = []

    # Pivot bench aggregates per (provider, model)
    bench_by_pm: dict[tuple[str, str], dict[str, Any]] = {}
    for row in agg_rows:
        key = (row[0], row[1])
        entry = bench_by_pm.setdefault(
            key,
            {"native_runs": 0, "native_ok": 0, "native_score": 0.0,
             "claude_runs": 0, "claude_ok": 0, "claude_score": 0.0,
             "latency_ms": 0, "last_bench": 0},
        )
        mode = row[2]
        runs = int(row[3] or 0)
        ok_runs = int(row[4] or 0)
        avg_score = float(row[5] or 0.0)
        avg_latency = int(row[6] or 0) if row[6] else 0
        last_bench = int(row[7] or 0)
        if mode == "native":
            entry["native_runs"] = runs
            entry["native_ok"] = ok_runs
            entry["native_score"] = avg_score
        elif mode == "claude":
            entry["claude_runs"] = runs
            entry["claude_ok"] = ok_runs
            entry["claude_score"] = avg_score
        if avg_latency:
            entry["latency_ms"] = (entry["latency_ms"] + avg_latency) // 2 if entry["latency_ms"] else avg_latency
        entry["last_bench"] = max(entry["last_bench"], last_bench)

    # Task results per (provider, model) — последние 50 строк
    results_by_pm: dict[tuple[str, str], list[dict[str, Any]]] = {}
    tokens_by_pm: dict[tuple[str, str], list[int]] = {}
    for row in recent_rows:
        key = (row[0], row[1])
        bucket = results_by_pm.setdefault(key, [])
        prompt_tokens = int(row[9]) if row[9] is not None else None
        completion_tokens = int(row[10]) if row[10] is not None else None
        total_tokens = int(row[11]) if row[11] is not None else None
        if total_tokens is not None:
            tokens_by_pm.setdefault(key, []).append(total_tokens)
        if len(bucket) >= 50:
            continue
        entry: dict[str, Any] = {
            "task_id": row[3],
            "mode": row[2],
            "sample_id": row[4] or "",
            "ok": bool(row[6]),
            "score": float(row[7] or 0.0),
            "latency_ms": int(row[5] or 0),
        }
        if prompt_tokens is not None:
            entry["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            entry["completion_tokens"] = completion_tokens
        if total_tokens is not None:
            entry["total_tokens"] = total_tokens
        bucket.append(entry)

    ranked: list[dict[str, Any]] = []
    for hrow in health_rows:
        provider = hrow[0]
        model_id = hrow[1]
        health_rate = float(hrow[6] or 0.0)
        latency_ms = int(hrow[7] or 0)
        bench = bench_by_pm.get((provider, model_id))
        native_runs = int(bench["native_runs"]) if bench else 0
        claude_runs = int(bench["claude_runs"]) if bench else 0
        native_score = float(bench["native_score"]) if bench else 0.0
        claude_score = float(bench["claude_score"]) if bench else 0.0
        if bench and bench["latency_ms"]:
            latency_ms = int(bench["latency_ms"])
        last_bench = int(bench["last_bench"]) if bench else 0
        if (native_runs + claude_runs) == 0 and not args.include_unbenchmarked:
            continue
        bench_score = (native_score * 0.65) + (claude_score * 0.35 if claude_runs else 0.0)
        latency_bonus = max(0.0, min(0.1, (6000 - latency_ms) / 60000.0)) if latency_ms else 0.0
        overall = health_rate * 0.45 + bench_score * 0.45 + latency_bonus
        status = "available"
        if health_rate < 0.75:
            status = "unstable"
        elif (native_runs + claude_runs) and bench_score < 0.6:
            status = "unstable"
        notes = "Reliable in recent health checks."
        if (native_runs + claude_runs):
            notes = f"Pass rate: native {native_score:.0%}"
            if claude_runs:
                notes += f", claude {claude_score:.0%}."
            else:
                notes += "."
        tokens_list = tokens_by_pm.get((provider, model_id)) or []
        avg_tokens = round(sum(tokens_list) / len(tokens_list)) if tokens_list else None
        ranked.append(
            {
                "score": overall,
                "latency_ms": latency_ms,
                "last_bench": last_bench,
                "task_results": results_by_pm.get((provider, model_id), []),
                "model": {
                    "model": model_id,
                    "provider": PROVIDER_LABELS.get(provider, provider),
                    "strengths": infer_strengths(model_id, native_score, claude_score, latency_ms),
                    "contextWindow": context_window_hint(model_id),
                    "status": status,
                    "notes": notes[:240],
                    "scores": {
                        "native": round(native_score, 3),
                        "claude": round(claude_score, 3) if claude_runs else None,
                        "overall": round(overall, 3),
                    },
                    "avg_total_tokens": avg_tokens,
                },
            }
        )
    ranked.sort(key=lambda item: (item["score"], -item["latency_ms"], item["last_bench"]), reverse=True)
    models = []
    for idx, item in enumerate(ranked[: args.limit], start=1):
        m = item["model"]
        m["rank"] = idx
        m["task_results"] = item["task_results"]
        models.append(m)
    return {
        "source": "smolevich-ai-bot",
        "updated_at": now_ts(),
        "tasks": tasks,
        "models": models,
    }


def tasks_payload(args: argparse.Namespace) -> dict[str, Any]:
    tasks = load_tasks(args.tasks_path)
    methodology = ""
    try:
        methodology = Path(args.methodology_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    return {
        "source": "smolevich-ai-bot",
        "updated_at": now_ts(),
        "tasks": tasks,
        "methodology_md": methodology,
    }


def publish(endpoint: str, payload: dict[str, Any]) -> None:
    token = os.environ.get("MODEL_LEADERBOARD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MODEL_LEADERBOARD_TOKEN is not set")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "smolevich-ai-bot-model-benchmark",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    log.info("PUT %s → %s", endpoint, body[:300])


# ---------------------------------------------------------------------------
# Purge / retention
# ---------------------------------------------------------------------------


def purge(args: argparse.Namespace) -> None:
    if kill_switch():
        log.info("BOT_BENCHMARK_DISABLED=1, skipping purge")
        return
    job_cutoff = now_ts() - max(1, args.jobs_retention_days) * 86400
    res_cutoff = now_ts() - max(1, args.results_retention_days) * 86400
    with connect(args.db) as conn:
        try:
            cur = conn.execute(
                "DELETE FROM model_benchmark_jobs WHERE status IN ('done', 'failed') AND updated_ts < ?",
                (job_cutoff,),
            )
            log.info("Purged %s terminal jobs", cur.rowcount or 0)
            cur = conn.execute(
                "DELETE FROM model_benchmark_jobs WHERE status = 'queued' AND attempts >= ? AND updated_ts < ?",
                (args.max_attempts, job_cutoff),
            )
            log.info("Purged %s exhausted-queued jobs", cur.rowcount or 0)
            cur = conn.execute(
                "DELETE FROM model_benchmark_results WHERE ts < ?",
                (res_cutoff,),
            )
            log.info("Purged %s results", cur.rowcount or 0)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
        conn.commit()
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Free-tier model benchmark queue")
    parser.add_argument("--db", default=DB_FILE)
    parser.add_argument("--tasks-path", default=DEFAULT_TASKS_PATH)
    parser.add_argument("--methodology-path", default=DEFAULT_METHODOLOGY_PATH)
    parser.add_argument("--datasets-dir", default=DEFAULT_DATASETS_DIR)
    parser.add_argument("--benchmark-root", default=DEFAULT_BENCHMARK_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_enqueue_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", default="all", choices=["all", *PROVIDERS.keys()])
        p.add_argument("--mode", default="all", choices=["all", "native", "claude"])
        p.add_argument("--models-per-provider", type=int, default=DEFAULT_MODELS_PER_PROVIDER)
        p.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
        p.add_argument("--batch-id", default="")

    def add_work_options(p: argparse.ArgumentParser, with_mode: bool = True) -> None:
        if with_mode:
            p.add_argument("--mode", default="all", choices=["all", "native", "claude"])
        p.add_argument("--max-jobs", type=int, default=DEFAULT_MAX_JOBS)
        p.add_argument("--native-workers", type=int, default=DEFAULT_NATIVE_WORKERS)
        p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
        p.add_argument("--claude-timeout", type=int, default=DEFAULT_CLAUDE_TIMEOUT)
        p.add_argument("--max-attempts", type=int, default=2)
        p.add_argument("--stale-after", type=int, default=3600)
        p.add_argument("--worker-id", default="")

    enqueue = sub.add_parser("enqueue", help="Create queued benchmark jobs")
    add_enqueue_options(enqueue)

    work = sub.add_parser("work", help="Claim and execute queued jobs")
    add_work_options(work)

    run_cmd = sub.add_parser("run", help="Enqueue + work in one pass")
    add_enqueue_options(run_cmd)
    add_work_options(run_cmd, with_mode=False)

    leaderboard = sub.add_parser("leaderboard", help="Build or publish leaderboard / methodology payload")
    leaderboard.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_BENCH_HOURS)
    leaderboard.add_argument("--limit", type=int, default=30)
    leaderboard.add_argument("--include-unbenchmarked", action="store_true")
    leaderboard.add_argument("--publish", action="store_true")
    leaderboard.add_argument("--publish-tasks", action="store_true")
    leaderboard.add_argument("--endpoint", default=LEADERBOARD_ENDPOINT)
    leaderboard.add_argument("--tasks-endpoint", default=TASKS_ENDPOINT)

    sub.add_parser("tasks", help="Print methodology + tasks payload")

    refresh = sub.add_parser("refresh-datasets", help="Refresh dataset samples in benchmark-datasets/")
    refresh.add_argument("--seed", type=int, default=0)
    refresh.add_argument("--only", action="append", default=[])

    purge_cmd = sub.add_parser("purge", help="Drop old jobs/results per retention policy")
    purge_cmd.add_argument("--jobs-retention-days", type=int, default=7)
    purge_cmd.add_argument("--results-retention-days", type=int, default=30)
    purge_cmd.add_argument("--max-attempts", type=int, default=2)

    return parser


def cmd_enqueue(args: argparse.Namespace) -> None:
    if kill_switch():
        log.info("BOT_BENCHMARK_DISABLED=1, skipping enqueue")
        return
    enqueue_jobs(args)


def cmd_refresh(args: argparse.Namespace) -> None:
    script = Path(__file__).resolve().parent / "scripts" / "refresh-benchmark-datasets.py"
    cmd = [sys.executable, str(script), "--out", args.datasets_dir]
    if args.seed:
        cmd += ["--seed", str(args.seed)]
    for name in args.only:
        cmd += ["--only", name]
    subprocess.check_call(cmd)


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "enqueue":
        cmd_enqueue(args)
    elif args.command == "work":
        print_summary(work_jobs(args))
    elif args.command == "run":
        cmd_enqueue(args)
        print_summary(work_jobs(args))
    elif args.command == "leaderboard":
        payload = leaderboard_payload(args)
        if args.publish_tasks:
            publish(args.tasks_endpoint, tasks_payload(args))
        elif args.publish:
            publish(args.endpoint, payload)
        else:
            print_json(payload)
    elif args.command == "tasks":
        print_json(tasks_payload(args))
    elif args.command == "refresh-datasets":
        cmd_refresh(args)
    elif args.command == "purge":
        purge(args)


if __name__ == "__main__":
    main()
