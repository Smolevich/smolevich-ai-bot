"""Потолки, которые провайдеры реально применяют к одному запросу.

Источник — их собственные ответы, снятые с боевого ключа, а не страница с тарифами.

Замер 2026-09-08 (`x-ratelimit-*` в ответе api.groq.com, ключ из
`/etc/socks-monitor/.groq_key`):

    allam-2-7b               requests 7000/мин, tokens 6000/мин
    openai/gpt-oss-120b      requests 1000/мин, tokens 8000/мин
    openai/gpt-oss-20b       requests 1000/мин, tokens 8000/мин
    qwen/qwen3.8-27b         requests 1000/мин, tokens 8000/мин
    qwen/qwen3.6-27b         requests 1000/мин, tokens 8000/мин
    groq/compound-mini       requests  250/сут, tokens 70000

Отдельный потолок на выходные токены (OTPM) в заголовках не приходит вообще — он
виден только в теле 429. С 04 по 08.09.2026 `qwen/qwen3.8-27b` вернул его 96 раз:
«on output tokens per minute (OTPM): Limit 1000, Requested 1024». Запрос, у которого
max_tokens больше этого числа, не пройдёт никогда, сколько ни повторяй, — а набор
задач просит 1024, 1536 и 2048.
"""

from __future__ import annotations

import re

# (провайдер, модель) → сколько выходных токенов провайдер согласен обещать за запрос.
OUTPUT_TOKEN_CEILING: dict[tuple[str, str], int] = {
    ("groq", "qwen/qwen3.8-27b"): 1000,
}

# Провайдер считает не только запрошенное, но и уже потраченное в текущей минуте,
# поэтому в потолок не упираемся вплотную.
CEILING_MARGIN = 0.1

OTPM_LIMIT_RE = re.compile(r"\(OTPM\):\s*Limit\s+(\d+)", re.IGNORECASE)


def output_ceiling(provider: str, model_id: str) -> int | None:
    """Известный потолок выходных токенов, или None — если провайдер о нём не говорил."""
    return OUTPUT_TOKEN_CEILING.get((provider, model_id))


def capped_max_tokens(provider: str, model_id: str, wanted: int) -> int:
    """Сколько токенов просить: не больше нужного и не больше потолка минус запас."""
    ceiling = output_ceiling(provider, model_id)
    if not ceiling:
        return wanted
    return max(1, min(wanted, int(ceiling * (1 - CEILING_MARGIN))))


def output_ceiling_from_error(text: str | None) -> int | None:
    """Вытащить потолок из тела 429 — так таблица выше и узнаёт, что устарела."""
    match = OTPM_LIMIT_RE.search(text or "")
    return int(match.group(1)) if match else None
