from __future__ import annotations

import json
import logging
import math
import sqlite3
import time

from agent.config import DB_FILE, PROVIDERS, PROVIDER_DEFAULT
from agent.text import sanitize_model_id

log = logging.getLogger(__name__)

class DB:
    @staticmethod
    def connectDb():
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def withRetry(fn, label, attempts=4):
        for i in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("locked" in msg or "readonly" in msg) and i < attempts - 1:
                    time.sleep(0.15 * (i + 1))
                    continue
                raise
        return None

    @staticmethod
    def ensure_schema():
        try:
            with DB.connectDb() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                try:
                    conn.execute("ALTER TABLE sessions ADD COLUMN tools_enabled INTEGER DEFAULT 1")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE sessions ADD COLUMN engine_mode TEXT DEFAULT 'native'")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE sessions ADD COLUMN last_session_id TEXT DEFAULT ''")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN message_count INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE request_log ADD COLUMN mode TEXT DEFAULT 'native'"
                    )
                except Exception:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE request_log ADD COLUMN delivered INTEGER DEFAULT 0"
                    )
                except Exception:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE request_log ADD COLUMN request_http_ms INTEGER DEFAULT 0"
                    )
                except Exception:
                    pass
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_request_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER,
                        uid INTEGER,
                        provider TEXT,
                        model TEXT,
                        operation TEXT,
                        input_size_bytes INTEGER DEFAULT 0,
                        output_size_bytes INTEGER DEFAULT 0,
                        latency_ms INTEGER DEFAULT 0,
                        ok INTEGER DEFAULT 0,
                        error TEXT
                    )
                    """
                )
                try:
                    conn.execute(
                        """
                        UPDATE users
                        SET message_count = (
                            SELECT COUNT(*)
                            FROM request_log rl
                            WHERE rl.uid = users.id
                        )
                        WHERE COALESCE(message_count, 0) = 0
                        """
                    )
                except Exception:
                    pass
                conn.commit()
        except Exception as e:
            log.error(f"DB ensure_schema: {e}")

    @staticmethod
    def update_and_check(uid, username):
        try:
            def op():
                with DB.connectDb() as conn:
                    res = conn.execute("SELECT is_allowed, COALESCE(message_count, 0) FROM users WHERE id = ?", (uid,)).fetchone()
                    if res:
                        conn.execute("UPDATE users SET username = ?, message_count = ? WHERE id = ?",
                                     (username, (res[1] or 0) + 1, uid))
                        allowed = res[0] == 1
                    else:
                        conn.execute("INSERT INTO users (id, username, is_allowed, prompt_tokens, completion_tokens, message_count) VALUES (?, ?, 0, 0, 0, 1)", (uid, username))
                        allowed = False
                    conn.commit()
                    return allowed
            return DB.withRetry(op, "update_and_check")
        except Exception as e:
            log.error(f"DB update_and_check: {e}")
            return False
    @staticmethod
    def set_allowed(uid, allowed=True):
        try:
            def op():
                with DB.connectDb() as conn:
                    conn.execute("UPDATE users SET is_allowed = ? WHERE id = ?", (1 if allowed else 0, uid))
                    conn.commit()
            DB.withRetry(op, "set_allowed")
        except Exception as e:
            log.error(f"DB set_allowed: {e}")
    @staticmethod
    def add_usage(uid, prompt, completion):
        try:
            def op():
                with DB.connectDb() as conn:
                    conn.execute("UPDATE users SET prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ? WHERE id = ?", (prompt, completion, uid))
                    conn.commit()
            DB.withRetry(op, "add_usage")
        except Exception as e:
            log.error(f"DB add_usage: {e}")
    @staticmethod
    def get_session(uid):
        try:
            with DB.connectDb() as conn:
                res = conn.execute("SELECT model, history_json, provider, tools_enabled, engine_mode, COALESCE(last_session_id, ''), COALESCE(profile, 'beginner') FROM sessions WHERE user_id = ?", (uid,)).fetchone()
                if res:
                    prov = res[2] or PROVIDER_DEFAULT
                    tools_enabled = (res[3] if len(res) > 3 else None)
                    if tools_enabled is None:
                        tools_enabled = 1 if PROVIDERS.get(prov, {}).get("supports_tools", True) else 0
                    engine_mode = (res[4] if len(res) > 4 else None) or "native"
                    last_session_id = (res[5] if len(res) > 5 else None) or ""
                    profile = (res[6] if len(res) > 6 else None) or "beginner"
                    return {"model": sanitize_model_id(res[0]), "history": json.loads(res[1]), "provider": prov, "tools_enabled": tools_enabled == 1, "engine_mode": engine_mode, "last_session_id": last_session_id, "profile": profile}
                # Brand-new session — pick historically best healthy text model; fall back to fastest healthy; then to static default.
                chosen = DB.pick_default_text_model(PROVIDER_DEFAULT)
                return {"model": chosen, "history": [], "provider": PROVIDER_DEFAULT, "tools_enabled": True, "engine_mode": "native", "last_session_id": "", "profile": "beginner"}
        except Exception as e:
            log.error(f"DB get_session: {e}")
            return {"model": PROVIDERS[PROVIDER_DEFAULT]["default_model"], "history": [], "provider": PROVIDER_DEFAULT, "tools_enabled": True, "engine_mode": "native", "last_session_id": "", "profile": "beginner"}
    @staticmethod
    def save_session(uid, model, history, provider=None, tools_enabled=True, engine_mode="native"):
        try:
            model = sanitize_model_id(model)
            def op():
                with DB.connectDb() as conn:
                    conn.execute(
                        """
                        INSERT INTO sessions (user_id, model, history_json, provider, tools_enabled, engine_mode)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            model=excluded.model,
                            history_json=excluded.history_json,
                            provider=excluded.provider,
                            tools_enabled=excluded.tools_enabled,
                            engine_mode=excluded.engine_mode
                        """,
                        (uid, model, json.dumps(history), provider or PROVIDER_DEFAULT, 1 if tools_enabled else 0, engine_mode or "native"),
                    )
                    conn.commit()
            DB.withRetry(op, "save_session")
        except Exception as e:
            log.error(f"DB save_session: {e}")
    @staticmethod
    def pick_default_text_model(provider):
        """Pick a sensible chat model for a brand-new session.
        1. Best-scoring model in request_log among ones currently healthy.
        2. Otherwise fastest available text model.
        3. Otherwise the provider's static default constant.
        """
        static_default = PROVIDERS.get(provider, {}).get("default_model", "")
        try:
            healthy_list = DB.get_healthy_models(provider, category="text", limit=50)
            if not healthy_list:
                return static_default
            healthy_ids = {m["id"] for m in healthy_list}
            for entry in DB.get_top_models(limit=20):
                if entry.get("provider") == provider and entry.get("model") in healthy_ids:
                    return entry["model"]
            return healthy_list[0]["id"]
        except Exception as e:
            log.error(f"DB pick_default_text_model: {e}")
            return static_default
    @staticmethod
    def pick_default_tts_model():
        """Pick the fastest available TTS model across known providers."""
        try:
            with DB.connectDb() as conn:
                row = conn.execute(
                    "SELECT provider, model_id FROM model_health "
                    "WHERE category='audio' AND available=1 AND capabilities LIKE '%audio:tts%' "
                    "ORDER BY latency_ms ASC LIMIT 1"
                ).fetchone()
                if row:
                    return row[0], row[1]
        except Exception as e:
            log.error(f"DB pick_default_tts_model: {e}")
        return None, None
    @staticmethod
    def pick_default_stt_model():
        """Pick the fastest available STT model across known providers.
        Returns (provider, model_id) or (None, None) if nothing healthy.
        """
        try:
            with DB.connectDb() as conn:
                row = conn.execute(
                    "SELECT provider, model_id FROM model_health "
                    "WHERE category='audio' AND available=1 AND capabilities LIKE '%audio:stt%' "
                    "ORDER BY latency_ms ASC LIMIT 1"
                ).fetchone()
                if row:
                    return row[0], row[1]
        except Exception as e:
            log.error(f"DB pick_default_stt_model: {e}")
        return None, None
    @staticmethod
    def save_profile(uid, profile):
        # Persist beginner/pro profile without touching the rest of the session state.
        # Insert a row if the user has none yet so the column survives across restarts.
        profile = profile if profile in ("beginner", "pro") else "beginner"
        try:
            def op():
                with DB.connectDb() as conn:
                    conn.execute(
                        """
                        INSERT INTO sessions (user_id, model, history_json, provider, tools_enabled, engine_mode, profile)
                        VALUES (?, '', '[]', ?, 1, 'native', ?)
                        ON CONFLICT(user_id) DO UPDATE SET profile=excluded.profile
                        """,
                        (uid, PROVIDER_DEFAULT, profile),
                    )
                    conn.commit()
            DB.withRetry(op, "save_profile")
        except Exception as e:
            log.error(f"DB save_profile: {e}")
    @staticmethod
    def get_healthy_models(provider, category="text", limit=10):
        try:
            with DB.connectDb() as conn:
                rows = conn.execute(
                    "SELECT model_id, latency_ms, supports_tools FROM model_health WHERE provider = ? AND category = ? AND available = 1 ORDER BY latency_ms ASC LIMIT ?",
                    (provider, category, limit)).fetchall()
                return [{"id": r[0], "latency_ms": r[1], "supportsTools": r[2] == 1} for r in rows]
        except Exception as e:
            log.error(f"DB get_healthy_models: {e}")
            return []
    @staticmethod
    def get_recent_models(provider, max_age_sec=600, category="text", limit=12):
        try:
            cutoff = int(time.time()) - max_age_sec
            with DB.connectDb() as conn:
                rows = conn.execute(
                    "SELECT model_id, latency_ms, available, supports_tools FROM model_health "
                    "WHERE provider = ? AND category = ? AND last_check >= ? "
                    "ORDER BY available DESC, latency_ms ASC LIMIT ?",
                    (provider, category, cutoff, limit)).fetchall()
                return [{"id": r[0], "latency_ms": r[1] or 0, "available": r[2] == 1, "supportsTools": r[3] == 1} for r in rows]
        except Exception as e:
            log.error(f"DB get_recent_models: {e}")
            return []
    @staticmethod
    def get_model_info(provider, model_id):
        try:
            with DB.connectDb() as conn:
                try:
                    row = conn.execute(
                        "SELECT latency_ms, available, supports_tools, category, last_check, capabilities FROM model_health WHERE provider = ? AND model_id = ?",
                        (provider, model_id)).fetchone()
                except sqlite3.OperationalError:
                    row = conn.execute(
                        "SELECT latency_ms, available, supports_tools, category, last_check FROM model_health WHERE provider = ? AND model_id = ?",
                        (provider, model_id)).fetchone()
                if row:
                    info = {
                        "latency_ms": row[0],
                        "available": row[1] == 1,
                        "supports_tools": row[2] == 1,
                        "category": row[3],
                        "last_check": row[4],
                    }
                    if len(row) > 5:
                        info["capabilities"] = row[5] or ""
                    return info
        except Exception as e:
            log.error(f"DB get_model_info: {e}")
        return None
    @staticmethod
    def log_request(uid, provider, model, prompt_tokens, completion_tokens, finish_reason, tool_calls, error=None, mode="native", request_http_ms=0):
        try:
            def op():
                with DB.connectDb() as conn:
                    cur = conn.execute(
                        "INSERT INTO request_log (ts, uid, provider, model, prompt_tokens, completion_tokens, finish_reason, tool_calls, error, mode, delivered, request_http_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (int(time.time()), uid, provider, model, prompt_tokens, completion_tokens, finish_reason, tool_calls, error, mode, int(request_http_ms or 0)))
                    conn.commit()
                    return cur.lastrowid
            return DB.withRetry(op, "log_request")
        except Exception as e:
            log.error(f"DB log_request: {e}")
            return None

    @staticmethod
    def log_media_request(uid, provider, model, operation, input_size_bytes=0, output_size_bytes=0, latency_ms=0, ok=False, error=None):
        try:
            def op():
                with DB.connectDb() as conn:
                    cur = conn.execute(
                        "INSERT INTO media_request_log (ts, uid, provider, model, operation, input_size_bytes, output_size_bytes, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(time.time()),
                            uid,
                            provider,
                            model,
                            operation,
                            int(input_size_bytes or 0),
                            int(output_size_bytes or 0),
                            int(latency_ms or 0),
                            1 if ok else 0,
                            (error or "")[:500],
                        ),
                    )
                    conn.commit()
                    return cur.lastrowid
            return DB.withRetry(op, "log_media_request")
        except Exception as e:
            log.error(f"DB log_media_request: {e}")
            return None

    @staticmethod
    def set_request_delivered(req_id, delivered=True):
        if not req_id:
            return
        try:
            def op():
                with DB.connectDb() as conn:
                    conn.execute(
                        "UPDATE request_log SET delivered = ? WHERE rowid = ?",
                        (1 if delivered else 0, req_id),
                    )
                    conn.commit()
            DB.withRetry(op, "set_request_delivered")
        except Exception as e:
            log.error(f"DB set_request_delivered: {e}")
    @staticmethod
    def get_all_users_stats():
        try:
                with DB.connectDb() as conn:
                    res = conn.execute("SELECT u.id, u.username, u.is_allowed, COALESCE(u.message_count, 0), u.prompt_tokens, u.completion_tokens FROM users u LEFT JOIN sessions s ON u.id = s.user_id").fetchall()
                    stats = []
                    for uid, uname, allowed, msg_count, pt, ct in res:
                        stats.append({"id": uid, "username": uname, "allowed": allowed == 1, "count": msg_count or 0, "prompt": pt or 0, "completion": ct or 0})
                    return stats
        except Exception as e:
            log.error(f"DB get_all_users_stats: {e}")
            return []

    @staticmethod
    def get_top_models(limit=3):
        try:
            with DB.connectDb() as conn:
                rows = conn.execute(
                    """
                    SELECT provider, model,
                           SUM(delivered_ok) AS delivered_answers,
                           COUNT(*) AS total_requests
                    FROM (
                        SELECT provider, model, CASE WHEN delivered = 1 THEN 1 ELSE 0 END AS delivered_ok
                        FROM request_log
                        UNION ALL
                        SELECT provider, model, CASE WHEN ok = 1 THEN 1 ELSE 0 END AS delivered_ok
                        FROM media_request_log
                    ) t
                    GROUP BY provider, model
                    ORDER BY delivered_answers DESC, total_requests DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                out = []
                for provider, model, delivered, total in rows:
                    delivered = delivered or 0
                    total = total or 0
                    rate = (delivered / total * 100.0) if total else 0.0
                    score = rate * math.log10(total + 1) if total else 0.0
                    out.append({
                        "provider": provider,
                        "model": model,
                        "delivered": delivered,
                        "total": total,
                        "success_rate": rate,
                        "score": score,
                    })
                out.sort(key=lambda x: (x["score"], x["delivered"], x["total"]), reverse=True)
                return out[:limit]
        except Exception as e:
            log.error(f"DB get_top_models: {e}")
            return []

    @staticmethod
    def get_top_providers(limit=3):
        try:
            with DB.connectDb() as conn:
                rows = conn.execute(
                    """
                    SELECT provider,
                           SUM(delivered_ok) AS delivered_answers,
                           COUNT(*) AS total_requests
                    FROM (
                        SELECT provider, CASE WHEN delivered = 1 THEN 1 ELSE 0 END AS delivered_ok
                        FROM request_log
                        UNION ALL
                        SELECT provider, CASE WHEN ok = 1 THEN 1 ELSE 0 END AS delivered_ok
                        FROM media_request_log
                    ) t
                    GROUP BY provider
                    ORDER BY delivered_answers DESC, total_requests DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                out = []
                for provider, delivered, total in rows:
                    delivered = delivered or 0
                    total = total or 0
                    rate = (delivered / total * 100.0) if total else 0.0
                    score = rate * math.log10(total + 1) if total else 0.0
                    out.append({
                        "provider": provider,
                        "delivered": delivered,
                        "total": total,
                        "success_rate": rate,
                        "score": score,
                    })
                out.sort(key=lambda x: (x["score"], x["delivered"], x["total"]), reverse=True)
                return out[:limit]
        except Exception as e:
            log.error(f"DB get_top_providers: {e}")
            return []

    @staticmethod
    def set_last_session_id(uid, session_id):
        try:
            def op():
                with DB.connectDb() as conn:
                    conn.execute(
                        "UPDATE sessions SET last_session_id = ? WHERE user_id = ?",
                        (session_id or "", uid),
                    )
                    conn.commit()
            DB.withRetry(op, "set_last_session_id")
        except Exception as e:
            log.error(f"DB set_last_session_id: {e}")
