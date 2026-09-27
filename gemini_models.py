"""Каталог моделей Gemini: цены, доступность в бесплатном тарифе, поддержка thinking.

Каталог захардкожен намеренно — цены через API не отдаются. Но список моделей
дополняется живым ответом `client.models.list()`, поэтому новые модели появятся
в интерфейсе сами, даже если этот файл устареет.

Источник цен: https://ai.google.dev/gemini-api/docs/pricing (проверено CATALOG_UPDATED).
Цены указаны в долларах за 1 миллион токенов, платный тариф, промпт до 200k токенов.
"""

import re
import logging
from gemini_quota import DEFAULT_LIMITS, has_free_quota

CATALOG_UPDATED = "2026-09-18"

# Модели, отключённые Google. Попытка обращения вернёт 404.
RETIRED_MODELS = {
    "gemini-3-pro-preview": "Отключена Google 09.03.2026. Замена: gemini-3.1-pro-preview (платная).",
    "gemini-2.0-flash": "Отключена Google. Замена: gemini-2.5-flash или gemini-3.5-flash.",
    "gemini-2.0-flash-lite": "Отключена Google. Замена: gemini-2.5-flash-lite.",
    "gemini-2.0-flash-exp": "Отключена Google. Замена: gemini-2.5-flash.",
    "gemini-1.5-pro": "Отключена Google. Замена: gemini-2.5-pro.",
    "gemini-1.5-flash": "Отключена Google. Замена: gemini-2.5-flash.",
    "gemini-1.5-flash-8b": "Отключена Google. Замена: gemini-2.5-flash-lite.",
    "gemini-pro": "Устаревшее имя. Замена: gemini-2.5-pro.",
}

# input_price / output_price — доллары за 1M токенов. None означает «цена неизвестна».
MODELS = [
    {
        "id": "gemma-4-31b-it", "label": "Gemma 4 31B", "family": "Gemma 4",
        "input_price": 0, "output_price": 0, "free_tier": True,
        "thinking": True, "status": "stable",
        "note": "Текст и изображения → текст. 30 RPM, 16 тыс. TPM, 14 400 RPD; небольшой минутный бюджет контекста. Google Search в тарифе API недоступен.",
    },
    {
        "id": "gemma-4-26b-a4b-it", "label": "Gemma 4 26B", "family": "Gemma 4",
        "input_price": 0, "output_price": 0, "free_tier": True,
        "thinking": True, "status": "stable",
        "note": "Текст и изображения → текст. 30 RPM, 16 тыс. TPM, 14 400 RPD; небольшой минутный бюджет контекста. Google Search в тарифе API недоступен.",
    },
    {
        "id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "family": "3.x",
        "input_price": 0.75, "output_price": 3.75, "free_tier": True,
        "thinking": True, "status": "stable", "note": "Новая сильная Flash-модель с бесплатным тарифом. Цены платного тарифа до конца 2026 года.",
    },
    {
        "id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "family": "3.x",
        "input_price": 0.75, "output_price": 3.75, "free_tier": True,
        "thinking": True, "status": "stable", "note": "Быстрая Flash-модель с бесплатным тарифом. Цены платного тарифа до конца 2026 года.",
    },
    {
        "id": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "family": "3.x",
        "input_price": 0.75,
        "output_price": 3.75,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Есть бесплатный тариф. Цены платного тарифа до конца 2026 года.",
    },
    {
        "id": "gemini-3.5-flash",
        "label": "Gemini 3.5 Flash",
        "family": "3.x",
        "input_price": 1.50,
        "output_price": 9.00,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Предшественник 3.6 Flash, дороже на выходе.",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "family": "3.x",
        "input_price": 0.30,
        "output_price": 2.50,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Дешёвая и быстрая, для простой болтовни хватает.",
    },
    {
        "id": "gemini-3.1-flash-lite",
        "label": "Gemini 3.1 Flash-Lite",
        "family": "3.x",
        "input_price": 0.25,
        "output_price": 1.50,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Самая дешёвая из линейки 3.x.",
    },
    {
        "id": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "family": "2.5",
        "input_price": 1.25,
        "output_price": 10.00,
        "free_tier": False,
        "thinking": True,
        "status": "stable",
        "note": "Платная модель; бесплатной квоты в профиле проекта нет.",
    },
    {
        "id": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "family": "2.5",
        "input_price": 0.30,
        "output_price": 2.50,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Есть бесплатный тариф; доступ новых проектов к 2.5 может быть ограничен Google.",
    },
    {
        "id": "gemini-2.5-flash-lite",
        "label": "Gemini 2.5 Flash-Lite",
        "family": "2.5",
        "input_price": 0.10,
        "output_price": 0.40,
        "free_tier": True,
        "thinking": True,
        "status": "stable",
        "note": "Самая дешёвая, используется для памяти. Доступ новых проектов к 2.5 может быть ограничен Google.",
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro (preview)",
        "family": "3.x",
        "input_price": 2.00,
        "output_price": 12.00,
        "free_tier": False,
        "thinking": True,
        "status": "preview",
        "note": "Флагман. Бесплатного тарифа нет — только за деньги.",
    },
    {
        "id": "gemini-3-flash-preview",
        "label": "Gemini 3 Flash (preview)",
        "family": "3.x",
        "input_price": 0.50,
        "output_price": 3.00,
        "free_tier": True,
        "thinking": True,
        "status": "preview",
        "note": "Есть бесплатный тариф. Google рекомендует переход на gemini-3.6-flash.",
    },
]

DEFAULT_MODEL = "gemini-3.6-flash"
MEMORY_MODEL = "gemini-3.5-flash-lite"

_BY_ID = {m["id"]: m for m in MODELS}

# Оценочный порядок: Pro и Flash — оранжевые, Flash-Lite — жёлтые.
QUALITY_ORDER = {
    "strong": ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash"),
    "light": ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-2.5-flash-lite"),
}

# Умеют generateContent, но это не текстовые модели для переписки: озвучка,
# картинки, музыка, транскрипция, агенты. В списке выбора им не место.
_NON_TEXT_MARKERS = (
    "tts", "image", "nano-banana", "omni", "transcribe", "lyria", "robotics",
    "computer-use", "antigravity", "deep-research", "embedding", "veo", "live",
    "native-audio", "aqa", "imagen",
)


def _normalize(model_id):
    """'models/gemini-2.0-flash-001' → 'gemini-2.0-flash': суффикс версии не меняет сути."""
    name = (model_id or "").strip().replace("models/", "")
    return re.sub(r"-\d{3}$", "", name)


def get_model_info(model_id):
    """Возвращает словарь с описанием модели или None, если она не в каталоге."""
    if not model_id:
        return None
    return _BY_ID.get(_normalize(model_id))


def is_retired(model_id):
    """Возвращает текст объяснения, если модель отключена Google, иначе None."""
    if not model_id:
        return None
    return RETIRED_MODELS.get(_normalize(model_id))


def is_text_model(model_id):
    """Подходит ли модель для текстовой переписки (а не озвучка/картинки/агенты)."""
    name = (model_id or "").lower()
    return not any(marker in name for marker in _NON_TEXT_MARKERS)


def supports_thinking(model_id):
    """Поддерживает ли модель ThinkingConfig.

    Для моделей из каталога берём значение оттуда. Для незнакомых — эвристика по
    имени: thinking есть в линейках 2.5 и 3.x. Если ошибёмся, API вернёт ошибку,
    которая будет показана пользователю.
    """
    info = get_model_info(model_id)
    if info is not None:
        return bool(info.get("thinking"))
    name = (model_id or "").lower()
    return "2.5" in name or name.startswith("gemini-3")


def format_price(info):
    """Короткая подпись с ценой для выпадающего списка."""
    if not info:
        return "цена неизвестна"
    inp, out = info.get("input_price"), info.get("output_price")
    if inp is None or out is None:
        return "цена неизвестна"
    return f"${inp:.2f} / ${out:.2f} за 1M"


def build_selector_options(live_model_ids=None):
    """Собирает список моделей для выпадающего списка в интерфейсе.

    Каталог идёт первым, затем — модели, которые API вернул, но которых нет в
    каталоге (у них будет пометка «цена неизвестна»). Отключённые модели
    исключаются.
    """
    options = []
    for info in MODELS:
        access_note = ("Доступ новых проектов ограничен"
                       if info['id'].startswith('gemini-2.5-') and info['id'] != 'gemini-2.5-pro'
                       else "")
        free_tier = info.get('free_tier')
        if info['id'] in DEFAULT_LIMITS:
            free_tier = has_free_quota(info['id'])
        elif free_tier:
            free_tier = None
        labels = ["Есть Free Tier" if free_tier is True else "Только платный тариф"
                  if free_tier is False else "Нет в вашей таблице бесплатных квот"]
        if access_note:
            labels.append(access_note)
        if info['id'].startswith('gemini-3'):
            labels.append("Google Search: платный проект")
        if info.get('status') == 'preview':
            labels.append("Preview")
        options.append({
            "id": info["id"],
            "label": info["label"],
            "price": format_price(info),
            "free_tier": free_tier,
            "quota_tracked": bool(free_tier and info['id'] in DEFAULT_LIMITS),
            "status": info.get("status"),
            "note": info.get("note", ""),
            "access_note": access_note,
            "availability_labels": labels,
            "known": True,
            "tier": ("light" if 'flash-lite' in info["id"] or info["id"].startswith('gemma-') else "strong") if free_tier else ("paid" if free_tier is False else "unknown"),
            "rank": next((order.index(info["id"]) for order in QUALITY_ORDER.values() if info["id"] in order), 999),
        })

    if live_model_ids:
        known = set(_BY_ID)
        for raw in live_model_ids:
            model_id = (raw or "").replace("models/", "").strip()
            if not model_id or model_id in known or is_retired(model_id) or not is_text_model(model_id):
                continue
            known.add(model_id)
            options.append({
                "id": model_id,
                "label": model_id,
                "price": "цена неизвестна",
                "free_tier": None,
                "status": "live",
                "note": "Найдена через API, в каталоге цен нет.",
                "access_note": "Доступ и квоты проекта не подтверждены",
                "availability_labels": ["Бесплатный тариф не подтверждён"],
                "known": False,
                "tier": "unknown",
                "rank": 999,
            })

    return options


def fetch_live_model_ids(client):
    """Забирает список доступных моделей у API. Ошибку глушит — это не критично."""
    if client is None:
        return []
    try:
        ids = []
        for model in client.models.list():
            name = getattr(model, "name", None)
            actions = getattr(model, "supported_actions", None)
            # Оставляем только те, что умеют генерировать текст.
            if actions and "generateContent" not in actions:
                continue
            if name:
                ids.append(name)
        return ids
    except Exception as e:
        logging.warning(f"Не удалось получить список моделей у API: {e}")
        return []
