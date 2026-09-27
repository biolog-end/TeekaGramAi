"""Поставщики API: Gemini и OpenAI.

Провайдер выводится из имени модели, поэтому в настройках чата по-прежнему хранится
одна строка model_name, а старые чаты работают без миграции.
"""

import gemini_models
import openai_models

_budget_catalog_synced = False

def budget():
    """Общая библиотека дневного бюджета OpenAI (openai_budget) или None, если не установлена.

    Она одна на машину (pip install -e ~/openai_budget) и держит счёт для всех проектов;
    без неё генерация работает, просто без проверки бесплатного лимита.
    """
    try:
        import openai_budget
        global _budget_catalog_synced
        if not _budget_catalog_synced:
            # Предложение этого аккаунта — единственный источник состава групп.
            # Согласуем счётчик с интерфейсом, сохранив запас/ключ и общий
            # файл конфигурации: эти переопределения действуют в процессе приложения.
            cfg = openai_budget.configure(persist=False)
            groups = dict(cfg.get('groups') or {})
            for group, info in openai_models.FREE_TIER_GROUPS.items():
                override = dict(groups.get(group) or {})
                override['limit'] = info['limit']
                override['models'] = sorted(info['models'])
                override.pop('add_models', None)
                groups[group] = override
            openai_budget.configure(groups=groups, persist=False)
            _budget_catalog_synced = True
        return openai_budget
    except ImportError:
        return None


PROVIDERS =("gemini", "openai")

LABELS = {"gemini": "Gemini", "openai": "OpenAI"}

_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5", "chatgpt-")


def provider_for_model(model_name):
    """'gpt-5-mini' → 'openai', 'gemini-2.5-flash' (и всё незнакомое) → 'gemini'."""
    name = (model_name or "").strip().lower()
    return "openai" if name.startswith(_OPENAI_PREFIXES) else "gemini"


def default_model(provider):
    return openai_models.DEFAULT_MODEL if provider == "openai" else gemini_models.DEFAULT_MODEL


def memory_model_for(model_name):
    """Модель для сжатия памяти — того же провайдера, что и модель чата,
    иначе память умирает, как только у второго провайдера нет ключей."""
    if provider_for_model(model_name) == "openai":
        return openai_models.MEMORY_MODEL
    return gemini_models.MEMORY_MODEL


def is_retired(model_name):
    if provider_for_model(model_name) == "openai":
        return None
    return gemini_models.is_retired(model_name)
