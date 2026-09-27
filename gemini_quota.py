"""Адаптер общей библиотеки gemini_budget; счётчики находятся вне этого проекта."""

import time
from pathlib import Path

try:
    import gemini_budget as _budget
    from gemini_budget.catalog import DEFAULT_LIMITS, PROFILE_DATE, PROFILE_SOURCE, CHAT_MODEL_IDS, has_free_quota
    from gemini_budget.core import _calendar, canonical_model, fingerprint
except ImportError as exc:
    raise ImportError(
        'Не установлена общая библиотека gemini_budget. Запустите start.bat или '
        'python -m pip install -e "%USERPROFILE%\\gemini_budget".'
    ) from exc

# Совместимый override для автономных тестов/preview. В работе путь общий для пользователя.
USAGE_FILE = str(_budget.state_path())
LEGACY_FILE = str(Path(__file__).resolve().parent / 'data' / 'gemini_usage.json')


def _state_file():
    if Path(USAGE_FILE).resolve() == _budget.state_path().resolve():
        _budget.import_legacy(LEGACY_FILE, state_file=USAGE_FILE)
    return USAGE_FILE


def snapshots(keys):
    rows = _budget.snapshots(keys, state_file=_state_file())
    return {key: [row for row in values if row['model'] in CHAT_MODEL_IDS
                  and has_free_quota(row['model'])] for key, values in rows.items()}


def reserve(key, model, input_tokens=0):
    return _budget.reserve(key, model, input_tokens, state_file=_state_file())


def finish(key, model, reservation, success=False, input_tokens=None, failure=None):
    return _budget.finish(key, model, reservation, success, input_tokens, failure, state_file=_state_file())


def correct(key, model, values):
    return _budget.correct(key, model, values, state_file=_state_file())
