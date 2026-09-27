"""Каталог моделей OpenAI: цены, бесплатные дневные группы, поддержка reasoning.

Как и gemini_models, каталог захардкожен (цены через API не отдаются) и дополняется
живым `client.models.list()`. Бесплатный дневной лимит действует только при включённой
в настройках организации OpenAI опции «share traffic»; считается он в токенах и по двум
группам моделей — их состав ниже.
"""

import re
import logging

CATALOG_UPDATED = "2026-09-16"

# Точный список из предложения в аккаунте пользователя (16.09.2026).
# Общая справка и доступность модели через API не расширяют это предложение.
FREE_TIER_GROUPS = {
    "large": {
        "limit": 250_000,
        "label": "250 тыс. токенов/день",
        "models": {"gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o", "o1", "o3"},
    },
    "small": {
        "limit": 2_500_000,
        "label": "2,5 млн токенов/день",
        "models": {"gpt-5.4-mini", "gpt-5.4-nano", "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini",
                   "gpt-4.1-nano", "gpt-4o-mini", "o3-mini", "o4-mini"},
    },
}

# input_price / output_price — Standard API, доллары за 1M токенов, обычный вход / выход.
# Источники: https://developers.openai.com/api/docs/pricing и страницы моделей.
# Для длинного контекста и других service tiers тариф может отличаться.
# reasoning — принимает ли модель параметр reasoning.effort.
MODELS = [
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "input_price": 4.00, "output_price": 20.00,
     "reasoning": True, "note": "Платная: вне предложения вашего аккаунта. Цена по акции как минимум до 21.11.2026."},
    {"id": "gpt-5.5", "label": "GPT-5.5", "input_price": 5.00, "output_price": 30.00,
     "reasoning": True, "note": "Платная: вне предложения вашего аккаунта."},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "input_price": 2.00, "output_price": 12.00,
     "reasoning": True, "note": "Платная: вне предложения вашего аккаунта."},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "input_price": 0.20, "output_price": 1.20,
     "reasoning": True, "note": "Платная: вне предложения вашего аккаунта."},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini", "input_price": 0.75, "output_price": 4.50,
     "reasoning": True, "note": "Рекомендуется по умолчанию: свежая, в бесплатной группе 2,5 млн токенов/день."},
    {"id": "gpt-5.4-nano", "label": "GPT-5.4 nano", "input_price": 0.20, "output_price": 1.25,
     "reasoning": True, "note": "Самая лёгкая из 5.4, бесплатная группа 2,5 млн/день."},
    {"id": "gpt-5.4", "label": "GPT-5.4", "input_price": 2.50, "output_price": 15.00,
     "reasoning": True, "note": "Флагман бесплатной группы 250 тыс. токенов/день."},
    {"id": "gpt-5.2", "label": "GPT-5.2", "input_price": 1.75, "output_price": 14.00,
     "reasoning": True, "note": "Бесплатная группа 250 тыс./день."},
    {"id": "gpt-5.1", "label": "GPT-5.1", "input_price": 1.25, "output_price": 10.00,
     "reasoning": True, "note": "Бесплатная группа 250 тыс./день."},
    {"id": "gpt-5", "label": "GPT-5", "input_price": 1.25, "output_price": 10.00,
     "reasoning": True, "note": "Бесплатная группа 250 тыс./день."},
    {"id": "gpt-5-mini", "label": "GPT-5 mini", "input_price": 0.25, "output_price": 2.00,
     "reasoning": True, "note": "Используется для сжатия памяти персонажа. Группа 2,5 млн/день."},
    {"id": "gpt-5-nano", "label": "GPT-5 nano", "input_price": 0.05, "output_price": 0.40,
     "reasoning": True, "note": "Самая дешёвая. Группа 2,5 млн/день."},
    {"id": "gpt-4.1", "label": "GPT-4.1", "input_price": 2.00, "output_price": 8.00,
     "reasoning": False, "note": "Без reasoning — быстрые ответы. Группа 250 тыс./день."},
    {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini", "input_price": 0.40, "output_price": 1.60,
     "reasoning": False, "note": "Без reasoning, быстрая. Группа 2,5 млн/день."},
    {"id": "gpt-4.1-nano", "label": "GPT-4.1 nano", "input_price": 0.10, "output_price": 0.40,
     "reasoning": False, "note": "Без reasoning, самая быстрая. Группа 2,5 млн/день."},
    {"id": "gpt-4o", "label": "GPT-4o", "input_price": 2.50, "output_price": 10.00,
     "reasoning": False, "note": "Группа 250 тыс./день."},
    {"id": "gpt-4o-mini", "label": "GPT-4o mini", "input_price": 0.15, "output_price": 0.60,
     "reasoning": False, "note": "Группа 2,5 млн/день."},
    {"id": "o3", "label": "o3", "input_price": 2.00, "output_price": 8.00,
     "reasoning": True, "note": "Reasoning-модель, медленная. Группа 250 тыс./день."},
    {"id": "o4-mini", "label": "o4-mini", "input_price": 1.10, "output_price": 4.40,
     "reasoning": True, "note": "Reasoning, группа 2,5 млн/день."},
    {"id": "o3-mini", "label": "o3-mini", "input_price": 1.10, "output_price": 4.40,
     "reasoning": True, "note": "Reasoning, бесплатная группа 2,5 млн токенов/день по предложению вашего аккаунта."},
    {"id": "o1-mini", "label": "o1-mini", "input_price": 1.10, "output_price": 4.40,
     "reasoning": True, "note": "Платная: вне предложения вашего аккаунта."},
    {"id": "o1", "label": "o1", "input_price": 15.00, "output_price": 60.00,
     "reasoning": True, "note": "Дорогая reasoning-модель. Группа 250 тыс./день."},
]

DEFAULT_MODEL = "gpt-5.4-mini"
MEMORY_MODEL = "gpt-5-mini"

_BY_ID = {m["id"]: m for m in MODELS}

# Примерный порядок качества внутри групп для интерфейса; цена и бесплатность от него не зависят.
QUALITY_ORDER = {
    "large": ("gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5", "o3", "gpt-4.1", "gpt-4o", "o1"),
    "small": ("gpt-5.4-mini", "gpt-5-mini", "o4-mini", "o3-mini", "gpt-4.1-mini", "gpt-5.4-nano", "gpt-4o-mini", "gpt-5-nano", "gpt-4.1-nano"),
    "paid": ("gpt-5.6-sol", "gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna", "o1-mini"),
}


def selector_rank(model_id):
    group = free_tier_group(model_id)
    if group:
        order = QUALITY_ORDER[group]
        return (0 if group == 'large' else 1, order.index(model_id) if model_id in order else len(order))
    order = QUALITY_ORDER['paid']
    return (3, order.index(model_id) if model_id in order else len(order))

# Модели OpenAI, которые не для текстовой переписки, устаревшие семейства и снапшоты
# (gpt-4.1-2025-04-14, gpt-3.5-turbo-0125, *-chat-latest) — дублируют базовое имя.
_NON_TEXT_MARKERS = (
    "audio", "realtime", "transcribe", "tts", "search", "image", "embedding", "moderation",
    "instruct", "codex", "whisper", "dall-e", "davinci", "babbage", "computer-use", "preview",
    "chatgpt-", "sora", "deep-research", "pro", "chat-latest", "gpt-3.5", "gpt-4-", "turbo",
)
_TEXT_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5")
_SNAPSHOT_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")


def _normalize(model_id):
    name = (model_id or "").strip().lower().removeprefix('models/')
    return re.sub(r'-\d{4}-\d{2}-\d{2}$', '', name)


def get_model_info(model_id):
    return _BY_ID.get(_normalize(model_id))


def supports_reasoning(model_id):
    """Принимает ли модель параметр reasoning. Для незнакомых — по имени: o-серия и gpt-5+."""
    info = get_model_info(model_id)
    if info is not None:
        return bool(info.get("reasoning"))
    name = _normalize(model_id).lower()
    return name.startswith(("o1", "o3", "o4", "o5")) or name.startswith("gpt-5")


def temperature_mode(model_id):
    """Подтверждённая поддержка: always, none (без reasoning), либо отсутствует.

    Не переносим поддержку флагманов на mini/nano или новые reasoning-модели.
    Источник: https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.4
    """
    name = _normalize(model_id)
    if name in ('gpt-5.1', 'gpt-5.2', 'gpt-5.4'):
        return 'none'
    if name.startswith(('gpt-4.1', 'gpt-4o', 'gpt-3.5-turbo')) and not supports_reasoning(name):
        return 'always'
    return None


def free_tier_group(model_id):
    """'large' | 'small' | None — к какой бесплатной дневной группе относится модель."""
    name = _normalize(model_id)
    for group, info in FREE_TIER_GROUPS.items():
        if name in info["models"]:
            return group
    return None


def is_text_model(model_id):
    name = (model_id or '').strip().lower().removeprefix('models/')
    if not name.startswith(_TEXT_PREFIXES) or name == "gpt-4":
        return False
    if any(marker in name for marker in _NON_TEXT_MARKERS):
        return False
    if _SNAPSHOT_RE.search(name):
        return False
    return True


def format_price(info):
    if not info:
        return "цена неизвестна"
    inp, out = info.get("input_price"), info.get("output_price")
    group = free_tier_group(info["id"])
    free = f" · бесплатно {FREE_TIER_GROUPS[group]['label']}" if group else ""
    if inp is None or out is None:
        return "цена неизвестна" + free
    return f"${inp:.2f} / ${out:.2f} за 1M{free}"


def build_selector_options(live_model_ids=None):
    """Каталог, затем модели из API, которых в каталоге нет."""
    options = []
    for info in MODELS:
        options.append({
            "id": info["id"],
            "label": info["label"],
            "price": format_price(info),
            "free_tier": free_tier_group(info["id"]) is not None,
            "status": "stable",
            "note": info.get("note", ""),
            "known": True,
            "provider": "openai",
            "tier": "strong" if free_tier_group(info["id"]) == "large" else ("light" if free_tier_group(info["id"]) == "small" else "paid"),
            "rank": selector_rank(info["id"])[1],
        })
    if live_model_ids:
        known = set(_BY_ID)
        for raw in live_model_ids:
            if not is_text_model(raw):
                continue
            model_id = _normalize(raw)
            if not model_id or model_id in known or not is_text_model(model_id):
                continue
            known.add(model_id)
            options.append({
                "id": model_id,
                "label": model_id,
                "price": "цена неизвестна",
                "free_tier": False,
                "status": "live",
                "note": "Платная: вне предложения вашего аккаунта. Найдена через API; цена ещё не проверена.",
                "known": False,
                "provider": "openai",
                "tier": "paid",
                "rank": 999,
            })
    return options


def fetch_live_model_ids(client):
    """Список моделей у API OpenAI. Ошибку глушит — это не критично."""
    if client is None:
        return []
    try:
        return [getattr(m, "id", None) for m in client.models.list() if getattr(m, "id", None)]
    except Exception as e:
        logging.warning(f"Не удалось получить список моделей OpenAI: {e}")
        return []
