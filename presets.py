"""Готовые наборы настроек чата.

Пресет меняет только те поля, которые относятся к его смыслу, — остальное
остаётся как было. Например, «Быстрый ответ» трогает задержки и опечатки, но не
сбрасывает выбранного персонажа, модель или доступ к медиа.
"""

import gemini_models

PRESETS = [
    {
        "id": "human",
        "name": "Живой человек",
        "icon": "🙂",
        "description": "Заметные паузы, опечатки и редкая потеря слов. Дольше отвечает, "
                       "зато переписка выглядит естественно.",
        "settings": {
            "base_thinking_delay_s_min": 1.5,
            "base_thinking_delay_s_max": 4.0,
            "typing_delay_ms_min": 45.0,
            "typing_delay_ms_max": 110.0,
            "max_typing_duration_s": 30.0,
            "substitution_chance": 0.006,
            "transposition_chance": 0.006,
            "skip_chance": 0.003,
            "duplication_chance": 0.003,
            "lower_chance": 0.08,
            "word_loss_chance": 0.02,
            "max_lost_words": 1,
            "sticker_choosing_delay_min": 2.0,
            "sticker_choosing_delay_max": 5.5,
            "auto_mode_initial_wait": 6.0,
        },
    },
    {
        "id": "fast",
        "name": "Быстрый ответ",
        "icon": "⚡",
        "description": "Почти без задержек и без опечаток. Удобно, когда нужно быстро "
                       "проверить персонажа или промпт.",
        "settings": {
            "base_thinking_delay_s_min": 0.3,
            "base_thinking_delay_s_max": 0.8,
            "typing_delay_ms_min": 10.0,
            "typing_delay_ms_max": 25.0,
            "max_typing_duration_s": 8.0,
            "substitution_chance": 0.0,
            "transposition_chance": 0.0,
            "skip_chance": 0.0,
            "duplication_chance": 0.0,
            "lower_chance": 0.0,
            "word_loss_chance": 0.0,
            "sticker_choosing_delay_min": 0.5,
            "sticker_choosing_delay_max": 1.5,
            "auto_mode_initial_wait": 2.0,
            "auto_mode_check_interval": 2.0,
        },
    },
    {
        "id": "cheap",
        "name": "Экономный",
        "icon": "💰",
        "description": "Самая дешёвая модель, короткая история, медиа игнорируются. "
                       "Заметно меньше расход токенов и реже упирается в лимиты.",
        "settings": {
            "model_name": "gemini-2.5-flash-lite",
            "num_messages_to_fetch": 30,
            "ignore_all_media": True,
            "enable_thinking": False,
            "enable_google_search": False,
            "enable_auto_memory": False,
        },
    },
    {
        "id": "quality",
        "name": "Максимальное качество",
        "icon": "💎",
        "description": "Умная модель, длинная история, режим размышления и поиск в Google. "
                       "Поиск у Gemini 3.x требует платного проекта API.",
        "settings": {
            "model_name": gemini_models.DEFAULT_MODEL,
            "num_messages_to_fetch": 120,
            "ignore_all_media": False,
            "can_see_photos": True,
            "can_see_videos": True,
            "can_see_audio": True,
            "can_see_files_pdf": True,
            "enable_thinking": True,
            "enable_google_search": True,
            "enable_auto_memory": True,
        },
    },
    {
        "id": "text_only",
        "name": "Только текст",
        "icon": "📝",
        "description": "Полностью игнорировать картинки, видео, голосовые и PDF. "
                       "Резко снижает расход токенов на чатах с кучей медиа.",
        "settings": {
            "ignore_all_media": True,
            "can_see_photos": False,
            "can_see_videos": False,
            "can_see_audio": False,
            "can_see_files_pdf": False,
        },
    },
]

_BY_ID = {p["id"]: p for p in PRESETS}


def get_preset(preset_id):
    """Возвращает пресет по идентификатору или None."""
    return _BY_ID.get(preset_id)


def apply_preset(preset_id, current_settings):
    """Накладывает пресет поверх текущих настроек.

    Returns:
        tuple: (новые настройки | None, текст ошибки | None)
    """
    preset = get_preset(preset_id)
    if not preset:
        return None, f"Пресет '{preset_id}' не найден."

    updated = dict(current_settings)
    updated.update(preset["settings"])
    return updated, None


def list_presets():
    """Пресеты для интерфейса — без самих значений настроек."""
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "icon": p["icon"],
            "description": p["description"],
            "changes": len(p["settings"]),
        }
        for p in PRESETS
    ]
