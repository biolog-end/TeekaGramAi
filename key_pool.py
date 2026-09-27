"""Пул API-ключей Gemini и OpenAI с автоматической ротацией при упирании в лимиты.

Зачем: у бесплатных тарифов жёсткие квоты. Когда ключ упирается в лимит, API отвечает
429. Пул ловит это, откладывает ключ (для этой модели) «на отдых» и повторяет тот же
запрос следующим ключом того же провайдера.

Провайдер запроса выводится из имени модели (providers.provider_for_model), у каждого
ключа есть поле provider. Квоты Google считаются на модель и на проект — ключи из одного
проекта делят общий лимит, ротация помогает только между проектами. У OpenAI лимиты
тоже на модель, а бесплатный дневной объём — в токенах по двум группам моделей;
для него ведётся учёт токенов за день.

Для моделей из пользовательской таблицы AI Studio общий gemini_quota заранее
проверяет RPM/TPM/RPD. Лимиты редактируемые; ответы Google остаются источником ошибок.
"""

import os
import re
import json
import time
import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import errors as genai_errors
import openai

import providers
import openai_models
import instance_paths
import gemini_quota
from auto_activity import emit
from shared_storage import write_json

API_KEYS_FILE = instance_paths.private_path('api_keys.json')

# Сколько ждать, если API не сказал точное время повтора.
DEFAULT_MINUTE_COOLDOWN_S = 60
# Нулевая квота и отсутствие доступа требуют изменения настроек проекта, а не
# ежеминутных повторов. Сохранение ключей сбрасывает эти паузы сразу.
ZERO_QUOTA_COOLDOWN_S = 30 * 60
MODEL_ACCESS_COOLDOWN_S = 5 * 60
# Запасной откат для суточной квоты, если не удалось вычислить полночь.
FALLBACK_DAILY_COOLDOWN_S = 3600
# Паузы между повторами одного запроса при 5xx («модель перегружена») — это не про
# ключ, поэтому сначала ждём и повторяем, и только потом берём следующий ключ.
SERVER_ERROR_RETRY_DELAYS = (2, 4, 8)
# OpenAI «нет кредитов»: проверяем раз в полчаса, чтобы пополненный счёт подхватился сам.
INSUFFICIENT_QUOTA_COOLDOWN_S = 30 * 60

_lock = threading.RLock()
_keys = []          # список словарей из api_keys.json
_state = {}         # key_id -> состояние (кулдауны, счётчики, последняя ошибка)
_clients = {}       # key_id -> клиент провайдера
_cursor = 0         # указатель round-robin
_fallback_client = None
_fallback_broken = False


# --------------------------------------------------------------------------
# Загрузка и сохранение
# --------------------------------------------------------------------------

def _blank_state():
    return {
        'cooldown_until': 0.0,        # кулдаун ключа целиком (невалидный ключ, insufficient_quota)
        'cooldown_reason': '',
        'model_cooldowns': {},        # model -> {'until': ts, 'reason': str}; квоты — на модель
        'last_error': '',
        'minute_hits': deque(maxlen=1000),
        'day_stamp': '',
        'day_count': 0,
        'tokens_today': 0,
        'tokens_by_model': {},
        'success_count': 0,
        'fail_count': 0,
    }


def _clean_provider(value):
    value = str(value or '').strip().lower()
    return value if value in providers.PROVIDERS else 'gemini'


def load_keys():
    """Читает ключи с диска. Формат: {"keys": [{id, label, key, enabled, provider}]}.

    Admin-ключ OpenAI живёт не здесь, а в общей библиотеке openai_budget (один на все
    проекты); старое поле openai_admin_key из файла переносится туда.
    """
    global _keys
    with _lock:
        try:
            with open(API_KEYS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            raw = data.get('keys', []) if isinstance(data, dict) else []
            legacy_admin = str(data.get('openai_admin_key') or '').strip() if isinstance(data, dict) else ''
        except FileNotFoundError:
            raw, legacy_admin = [], ''
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Не удалось прочитать {API_KEYS_FILE}: {e}")
            raw, legacy_admin = [], ''

        budget = providers.budget()
        if legacy_admin and budget and not budget.get_admin_key():
            budget.set_admin_key(legacy_admin)
            logging.info("Admin-ключ OpenAI перенесён в общий конфиг openai_budget.")

        cleaned = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or not item.get('key'):
                continue
            key_id = str(item.get('id') or f'key{i + 1}')
            cleaned.append({
                'id': key_id,
                'label': item.get('label') or f'Ключ {i + 1}',
                'key': item['key'].strip(),
                'enabled': bool(item.get('enabled', True)),
                'provider': _clean_provider(item.get('provider')),
            })
            _state.setdefault(key_id, _blank_state())

        _keys = cleaned
        logging.info(f"Пул ключей: загружено {len(_keys)} шт. из {API_KEYS_FILE}")
        return _keys


def save_keys(keys, openai_admin_key=None):
    """Сохраняет ключи на диск и сбрасывает кэш клиентов.

    openai_admin_key: None — не трогать, '' — убрать, иначе — записать в общий конфиг
    openai_budget (в api_keys.json он не хранится).
    """
    global _keys
    with _lock:
        os.makedirs(os.path.dirname(API_KEYS_FILE), exist_ok=True)
        normalized = [
            {
                'id': k['id'],
                'label': k.get('label', ''),
                'key': k['key'],
                'enabled': bool(k.get('enabled', True)),
                'provider': _clean_provider(k.get('provider')),
            }
            for k in keys
        ]
        if openai_admin_key is not None:
            budget = providers.budget()
            if budget:
                budget.set_admin_key(str(openai_admin_key).strip())
            else:
                logging.warning("openai_budget не установлен — Admin-ключ OpenAI некуда сохранить.")
        try:
            write_json(API_KEYS_FILE, {'keys': normalized})
        except IOError as e:
            logging.error(f"Не удалось сохранить {API_KEYS_FILE}: {e}")
            return False

        _keys = normalized
        _clients.clear()
        for k in normalized:
            st = _state.setdefault(k['id'], _blank_state())
            # Сохранили ключи — значит, что-то поменяли (баланс, ключ): даём им новый шанс.
            st['cooldown_until'] = 0.0
            st['cooldown_reason'] = ''
            st['model_cooldowns'] = {}
        return True


def import_from_env():
    """Подхватывает ключи из .env для провайдеров, у которых ключей ещё нет.

    GOOGLE_API_KEYS / GOOGLE_API_KEY → Gemini, OPENAI_API_KEYS / OPENAI_API_KEY → OpenAI.
    """
    if instance_paths.IS_MANAGED:
        return False
    with _lock:
        present = {k['provider'] for k in _keys}

    env_sources = {
        'gemini': (('GOOGLE_API_KEYS', 'GOOGLE_API_KEY'), 'env', 'Из .env'),
        'openai': (('OPENAI_API_KEYS', 'OPENAI_API_KEY'), 'envoa', 'OpenAI из .env'),
    }
    new_keys = []
    for provider, (names, id_prefix, label) in env_sources.items():
        if provider in present:
            continue
        raw = ''
        for name in names:
            raw = os.getenv(name, '')
            if raw:
                break
        parts = [p.strip() for p in re.split(r'[,\s]+', raw) if p.strip()]
        new_keys.extend(
            {'id': f'{id_prefix}{i + 1}', 'label': f'{label} #{i + 1}', 'key': p,
             'enabled': True, 'provider': provider}
            for i, p in enumerate(parts)
        )
    if not new_keys:
        return False

    with _lock:
        combined = list(_keys) + new_keys
    save_keys(combined)
    logging.info(f"Пул ключей: импортировано {len(new_keys)} шт. из переменных окружения.")
    return True


# --------------------------------------------------------------------------
# Разбор ошибок квоты
# --------------------------------------------------------------------------

def _pacific_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        # tzdata может отсутствовать на Windows — берём фиксированный UTC-8.
        return timezone(timedelta(hours=-8))


def _day_stamp(provider):
    """Календарный день, по которому провайдер сбрасывает суточные счётчики.

    OpenAI обнуляет бесплатные токены в 00:00 UTC, Google — в полночь по Тихому океану.
    По местному времени считать нельзя: в Москве день OpenAI кончается в 03:00.
    """
    tz = timezone.utc if provider == 'openai' else _pacific_tz()
    return datetime.now(tz).strftime('%Y-%m-%d')


def _next_pacific_midnight_ts():
    """Момент сброса суточной квоты Google — полночь по тихоокеанскому времени."""
    tz = _pacific_tz()
    try:
        now = datetime.now(tz)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=30, microsecond=0)
        return tomorrow.timestamp()
    except Exception:
        return time.time() + FALLBACK_DAILY_COOLDOWN_S


def _parse_duration(value):
    """Длительность RetryInfo: строка '40.5s' или seconds/nanos; иначе None."""
    if isinstance(value, dict):
        try:
            return max(float(value.get('seconds', 0)) + float(value.get('nanos', 0)) / 1e9, 0)
        except (TypeError, ValueError):
            return None
    match = re.match(r'^\s*(\d+(?:\.\d+)?)\s*s?\s*$', str(value or ''))
    return float(match.group(1)) if match else None


def _gemini_error_items(err):
    """SDK хранит в APIError.details весь ответ; поддерживаем также details[]."""
    payload = getattr(err, 'details', None)
    if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
        payload = payload['error']
    if isinstance(payload, dict):
        payload = payload.get('details', [])
    return [item for item in payload or [] if isinstance(item, dict)] if isinstance(payload, list) else []


def _quota_details(err):
    """Отчёт Google о квоте: модель, метрика, период, явное значение и RetryInfo.

    Отсутствующее quotaValue не означает ноль. Суточная квота имеет приоритет
    над минутной, если Google вернул несколько нарушений одновременно.
    """
    report = {'model': None, 'scope': 'unknown', 'retry_s': None,
              'zero_limit': False, 'violations': [], 'reasons': []}
    for item in _gemini_error_items(err):
        reason = item.get('reason')
        if reason:
            report['reasons'].append(str(reason))
        violations = item.get('violations') or []
        metadata = item.get('metadata')
        if isinstance(metadata, dict) and any(k in metadata for k in ('quota_metric', 'quota_limit', 'quota_limit_value')):
            violation = {'quotaMetric': metadata.get('quota_metric'),
                         'quotaId': metadata.get('quota_limit'),
                         'quotaDimensions': {'model': metadata.get('model'), 'unit': metadata.get('quota_unit')}}
            if 'quota_limit_value' in metadata:
                violation['quotaValue'] = metadata['quota_limit_value']
            violations = [*violations, violation]
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            clean = {k: violation[k] for k in ('quotaId', 'quotaMetric', 'quotaDimensions', 'quotaValue') if k in violation}
            report['violations'].append(clean)
            dims = violation.get('quotaDimensions') or {}
            if not isinstance(dims, dict):
                dims = {}
            report['model'] = report['model'] or dims.get('model')
            metric = ' '.join(str(violation.get(k) or '') for k in ('quotaId', 'quotaMetric')) + ' ' + str(dims.get('unit') or '')
            if re.search(r'per[ _]*day|/d(?:ay)?\b', metric, re.IGNORECASE):
                report['scope'] = 'day'
            elif report['scope'] == 'unknown' and re.search(r'per[ _]*minute|/min\b', metric, re.IGNORECASE):
                report['scope'] = 'minute'
            if 'quotaValue' in violation:
                try:
                    report['zero_limit'] |= float(violation['quotaValue']) == 0
                except (TypeError, ValueError):
                    pass
        if 'retryDelay' in item:
            delay = _parse_duration(item['retryDelay'])
            if delay is not None:
                report['retry_s'] = max(report['retry_s'] or 0, delay)

    # Старые ответы иногда содержат только текст с quotaId, limit и retryDelay.
    text = str(getattr(err, 'message', '') or err)
    if report['scope'] == 'unknown':
        if re.search(r'per\s*day|PerDay', text, re.IGNORECASE):
            report['scope'] = 'day'
        elif re.search(r'per\s*minute|PerMinute', text, re.IGNORECASE):
            report['scope'] = 'minute'
    report['zero_limit'] |= bool(re.search(r'\blimit\s*[:=]\s*0(?:\.0+)?(?=\s|[,;]|\.?(?:\s|$))', text, re.IGNORECASE))
    if report['retry_s'] is None:
        match = re.search(r'retry\w*[\'"]?\s*(?:[:=]|in)\s*[\'"]?(\d+(?:\.\d+)?)\s*s', text, re.IGNORECASE)
        if match:
            report['retry_s'] = float(match.group(1))
    if report['model'] is None:
        match = re.search(r'model[\'"]?\s*:\s*[\'"]?([\w.\-]+)', text)
        if match:
            report['model'] = match.group(1)
    return report


def _parse_quota_error(err, report=None):
    """Определяет по ошибке 429 Gemini, какая квота исчерпана и сколько ждать.

    Returns:
        tuple: (model | None, cooldown_until_ts, человекочитаемая причина)
    """
    report = report if report is not None else _quota_details(err)
    model, scope, retry_s = report['model'], report['scope'], report['retry_s']
    if report['zero_limit']:
        return model, time.time() + ZERO_QUOTA_COOLDOWN_S, (
            'квота модели для этого проекта равна 0; проверьте доступ и тариф в AI Studio. '
            'Автоматическая перепроверка через 30 мин; сохранение ключей снимает паузу')
    matching = [v for v in report['violations'] if scope == 'unknown' or re.search(
        r'per[ _]*day|/d(?:ay)?\b' if scope == 'day' else r'per[ _]*minute|/min\b',
        str(v.get('quotaId') or '') + ' ' + str(v.get('quotaMetric') or '') + ' ' + str(v.get('quotaDimensions') or ''), re.IGNORECASE)]
    metrics = ' '.join(str(v.get('quotaMetric') or v.get('quotaId') or '') for v in matching)
    if not metrics:
        metrics = str(getattr(err, 'message', '') or '')
    tokens = bool(re.search(r'token', metrics, re.IGNORECASE))
    requests = bool(re.search(r'request', metrics, re.IGNORECASE))
    unit = 'токенов' if tokens and not requests else 'запросов' if requests and not tokens else ''
    if scope == 'day':
        abbreviation = 'TPD' if unit == 'токенов' else 'RPD' if unit == 'запросов' else 'тип метрики не указан'
        return model, _next_pacific_midnight_ts(), f'исчерпана суточная квота ({abbreviation}); сброс в полночь Pacific'
    if scope == 'unknown' and retry_s is None:
        return model, 0, 'Google вернул 429 без типа квоты и срока повтора; локальная пауза не назначена'
    wait = max(retry_s, 1.0) if retry_s is not None else DEFAULT_MINUTE_COOLDOWN_S
    if scope == 'minute':
        abbreviation = 'TPM' if unit == 'токенов' else 'RPM' if unit == 'запросов' else 'тип метрики не указан'
        reason = f'исчерпана минутная квота ({abbreviation})'
    else:
        reason = 'Google вернул 429, но не указал тип квоты'
    return model, time.time() + wait, f'{reason}; повтор через {wait:g} с'


def _openai_wait_seconds(exc):
    """Сколько ждать после 429 OpenAI: заголовок retry-after или текст
    «Please try again in 1.5s / 20s / 1m30s»."""
    headers = getattr(getattr(exc, 'response', None), 'headers', None)
    raw = headers.get('retry-after') if headers else None
    if raw and str(raw).strip().replace('.', '', 1).isdigit():
        return float(raw)
    text = str(getattr(exc, 'message', '') or exc)
    match = re.search(r'try again in\s*(?:(\d+)m)?\s*(\d+(?:\.\d+)?)\s*(ms|s)\b', text, re.IGNORECASE)
    if match:
        minutes = int(match.group(1) or 0)
        value = float(match.group(2))
        seconds = value / 1000.0 if match.group(3).lower() == 'ms' else value
        return minutes * 60 + seconds
    return None


def _fmt_wait(seconds):
    seconds = max(int(seconds), 0)
    if seconds >= 3600:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    if seconds >= 90:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


def _looks_like_bad_key(code, message):
    """Похоже ли, что ключ Gemini невалидный, а не просто упёрся в лимит."""
    if code not in (400, 401, 403):
        return False
    text = (message or '').lower()
    markers = ('api key not valid', 'api_key_invalid', 'invalid api key',
               'api key expired', 'api key was reported as leaked')
    return any(m in text for m in markers)


# --------------------------------------------------------------------------
# Единая форма ошибки API любого провайдера
# --------------------------------------------------------------------------

def _failure(code, message, kind='other', model=None, until=0.0, reason=''):
    """kind: 'quota' — ждать (ключ или модель), 'bad_key' — выключить ключ,
    'model_unavailable' — попробовать другой проект, 'server' — повторить,
    'other' — вернуть ошибку сразу."""
    return {'code': code, 'message': message, 'kind': kind,
            'model': model, 'until': until, 'reason': reason}


def _classify_gemini(exc, model):
    if not isinstance(exc, genai_errors.APIError):
        return None
    code = getattr(exc, 'code', None)
    message = getattr(exc, 'message', None) or str(exc)
    items = _gemini_error_items(exc)
    def failure_result(*args):
        failure = _failure(code, message, *args)
        failure['status'] = getattr(exc, 'status', None)
        failure['api_details'] = items
        return failure
    if code == 429:
        report = _quota_details(exc)
        err_model, until, reason = _parse_quota_error(exc, report)
        # Google может назвать внутренний alias; блокировать нужно модель запроса.
        failure = failure_result('quota', model or err_model, until, reason)
        failure['report'] = report
        return failure
    reasons = {str(item.get('reason') or '') for item in items}
    if _looks_like_bad_key(code, message) or (code in (400, 401, 403) and reasons & {'API_KEY_INVALID', 'API_KEY_EXPIRED'}):
        return failure_result('bad_key')
    model_error = reasons & {'MODEL_NOT_FOUND', 'MODEL_ACCESS_DENIED', 'MODEL_NOT_AVAILABLE'} or re.search(
        r'(?:\bmodels/[^\s]+|\bmodel\b)[^\n]*(?:not found|not available|no longer available|not supported|does not exist|access denied|permission denied)',
        message, re.IGNORECASE)
    if code in (403, 404) and model and model_error:
        explanation = 'модель недоступна этому проекту'
        if 'new users' in message.lower():
            explanation += '; Google ограничивает её для новых пользователей/проектов — выберите новую модель'
        return failure_result('model_unavailable', model, time.time() + MODEL_ACCESS_COOLDOWN_S,
                              f'{explanation}: {message}. Перепроверка через 5 мин')
    if code in (500, 502, 503, 504):
        return failure_result('server')
    return failure_result()


def _classify_openai(exc, model):
    if isinstance(exc, openai.APIConnectionError):
        return _failure(None, str(exc), 'server')
    if not isinstance(exc, openai.APIStatusError):
        return None
    code = getattr(exc, 'status_code', None)
    message = getattr(exc, 'message', None) or str(exc)
    err_code = str(getattr(exc, 'code', '') or '')
    err_type = str(getattr(exc, 'type', '') or '')
    if code == 429:
        # Реальный ответ без кредитов: type=insufficient_quota, code=credit_balance_exhausted.
        if err_type == 'insufficient_quota' or err_code in ('insufficient_quota', 'credit_balance_exhausted') \
                or 'insufficient_quota' in message:
            # Баланс кончился или дневной бесплатный объём выбран — это про ключ, не про модель.
            # Пауза короткая: после пополнения счёта ключ должен ожить сам.
            return _failure(code, message, 'quota', None, time.time() + INSUFFICIENT_QUOTA_COOLDOWN_S,
                            'нет кредитов (insufficient_quota) — пополните баланс на '
                            'platform.openai.com/settings/organization/billing или включите '
                            '«share traffic with OpenAI» ради бесплатного дневного лимита')
        wait = _openai_wait_seconds(exc) or DEFAULT_MINUTE_COOLDOWN_S
        return _failure(code, message, 'quota', model, time.time() + max(wait, 1.0),
                        f'лимит запросов (RPM/TPM), повтор через {_fmt_wait(wait)}')
    if code == 401:
        return _failure(code, message, 'bad_key')
    if code is not None and code >= 500:
        return _failure(code, message, 'server')
    return _failure(code, message)


def _classify(exc, provider, model):
    if provider == 'openai':
        return _classify_openai(exc, model)
    return _classify_gemini(exc, model)


_HINTS = {
    'gemini': {
        400: "Неверный запрос — проверьте generation_log.txt.",
        403: "Доступ запрещён: ключ не подходит или у него нет прав.",
        404: "Модель или ресурс не найдены — проверьте имя модели и приложенные файлы.",
        429: "Квоты API исчерпаны.",
        500: "Внутренняя ошибка сервера Gemini.",
        503: "Сервис Gemini временно недоступен.",
        504: "Gemini не успел завершить ответ до срока ожидания. Попробуйте меньший контекст или следующий ключ/модель.",
    },
    'openai': {
        400: "Неверный запрос — проверьте generation_log.txt (например, модель не принимает reasoning).",
        403: "Доступ запрещён: регион или права ключа.",
        404: "Модель не найдена или недоступна для этого ключа.",
        429: "Лимиты OpenAI исчерпаны.",
        500: "Внутренняя ошибка сервера OpenAI.",
        503: "Сервис OpenAI временно недоступен.",
    },
}


def _describe_failure(failure, provider):
    """Человекочитаемое описание ошибки API."""
    code = failure['code']
    hint = _HINTS.get(provider, {}).get(code, '')
    detail = failure.get('reason') or hint
    report = failure.get('report')
    suffix = f" Отчёт квоты Google: {json.dumps(report, ensure_ascii=False)}" if report else ''
    if provider == 'gemini':
        suffix += f" status={failure.get('status')}; details={json.dumps(failure.get('api_details', []), ensure_ascii=False)}"
    return f"Ошибка {providers.LABELS[provider]} {code}: {failure['message']}{(' ' + detail) if detail else ''}{suffix}"


# --------------------------------------------------------------------------
# Состояние ключей
# --------------------------------------------------------------------------

def _provider_of(key_id):
    for k in _keys:
        if k['id'] == key_id:
            return k['provider']
    return 'gemini'


def _roll_day(st, provider):
    today = _day_stamp(provider)
    if st['day_stamp'] != today:
        st['day_stamp'] = today
        st['day_count'] = 0
        st['tokens_today'] = 0
        st['tokens_by_model'] = {}


def _record_request(key_id):
    st = _state[key_id]
    st['minute_hits'].append(time.time())
    _roll_day(st, _provider_of(key_id))
    st['day_count'] += 1


def _record_usage(key_id, model, tokens):
    if not tokens:
        return
    st = _state[key_id]
    _roll_day(st, _provider_of(key_id))
    st['tokens_today'] += int(tokens)
    if model:
        st['tokens_by_model'][model] = st['tokens_by_model'].get(model, 0) + int(tokens)


def _requests_last_minute(key_id):
    st = _state[key_id]
    cutoff = time.time() - 60
    while st['minute_hits'] and st['minute_hits'][0] < cutoff:
        st['minute_hits'].popleft()
    return len(st['minute_hits'])


def _get_client(key_entry):
    key_id = key_entry['id']
    client = _clients.get(key_id)
    if client is None:
        if key_entry['provider'] == 'openai':
            # max_retries=0: иначе SDK сам молча повторяет 429/5xx и прячет их от пула.
            client = openai.OpenAI(api_key=key_entry['key'], max_retries=0)
        else:
            client = genai.Client(api_key=key_entry['key'])
        _clients[key_id] = client
    return client


def mask_key(key):
    if not key:
        return ''
    if len(key) <= 12:
        return key[:3] + '…'
    return f"{key[:6]}…{key[-4:]}"


def _free_tier_usage(st):
    """Сколько токенов за день ушло в каждую бесплатную группу OpenAI."""
    usage = {}
    for group, info in openai_models.FREE_TIER_GROUPS.items():
        used = sum(t for m, t in st['tokens_by_model'].items()
                   if openai_models.free_tier_group(m) == group)
        usage[group] = {'used': used, 'limit': info['limit'], 'label': info['label']}
    return usage


# --------------------------------------------------------------------------
# Дневной бюджет OpenAI — делегируется общей библиотеке openai_budget
# --------------------------------------------------------------------------

BUDGET_MISSING_HINT = ("Библиотека openai_budget не установлена: "
                       "pip install -e %USERPROFILE%\\openai_budget (start.bat делает это сам).")


def get_openai_admin_key():
    budget = providers.budget()
    return budget.get_admin_key() if budget else ''


def openai_admin_key_masked():
    budget = providers.budget()
    return budget.admin_key_masked() if budget else ''


def fetch_openai_org_usage():
    """Потребление организации OpenAI за сегодня (UTC) — свежий снимок + локальный счёт.

    Returns:
        tuple: (status dict из openai_budget.status() | None, error_message | None)
    """
    budget = providers.budget()
    if not budget:
        return None, BUDGET_MISSING_HINT
    _, err = budget.sync(force=True)
    if err:
        return None, err
    return budget.status(), None


def get_status():
    """Состояние пула для интерфейса. Сами ключи наружу не отдаются."""
    with _lock:
        now = time.time()
        items = []
        quota_error = ''
        try:
            shared_usage = gemini_quota.snapshots([k for k in _keys if k['provider'] == 'gemini'])
        except Exception as exc:
            shared_usage = {}
            quota_error = f'Не удалось прочитать общий учёт Gemini: {exc}'
        for k in _keys:
            st = _state.get(k['id']) or _blank_state()
            _roll_day(st, k['provider'])
            cooling = st['cooldown_until'] > now
            model_cooldowns = [
                {'model': m, 'left_s': int(v['until'] - now), 'reason': v['reason']}
                for m, v in st.get('model_cooldowns', {}).items() if v['until'] > now
            ]
            item = {
                'id': k['id'],
                'label': k.get('label', ''),
                'masked': mask_key(k['key']),
                'enabled': bool(k.get('enabled', True)),
                'provider': k['provider'],
                'cooling_down': cooling,
                'cooldown_left_s': int(st['cooldown_until'] - now) if cooling else 0,
                'cooldown_reason': st['cooldown_reason'] if cooling else '',
                'model_cooldowns': model_cooldowns,
                'last_error': st['last_error'],
                'requests_last_minute': _requests_last_minute(k['id']),
                'requests_today': st['day_count'],
                'tokens_today': st['tokens_today'],
                'success_count': st['success_count'],
                'fail_count': st['fail_count'],
            }
            if k['provider'] == 'openai':
                item['free_tier'] = _free_tier_usage(st)
            else:
                item['gemini_usage'] = shared_usage.get(k['id'], [])
                for row in item['gemini_usage']:
                    if row['blocked']:
                        item['model_cooldowns'].append({'model': row['model'], 'left_s': row['left_s'],
                                                       'reason': row['reason']})
            items.append(item)

        usable = [i for i in items if i['enabled'] and not i['cooling_down']]
        by_provider = {p: {'total': 0, 'available': 0} for p in providers.PROVIDERS}
        for item in items:
            by_provider[item['provider']]['total'] += 1
        for item in usable:
            by_provider[item['provider']]['available'] += 1

        # Модель «заблокирована», если её квота исчерпана на каждом рабочем ключе её провайдера.
        blocked_models = {}
        for provider in providers.PROVIDERS:
            usable_here = [i for i in usable if i['provider'] == provider]
            per_model = {}
            for item in usable_here:
                for mc in item['model_cooldowns']:
                    per_model.setdefault(mc['model'], []).append(mc)
            for m, entries in per_model.items():
                if usable_here and len(entries) == len(usable_here):
                    blocked_models[m] = {'left_s': min(e['left_s'] for e in entries),
                                         'reason': entries[0]['reason']}
        return {
            'keys': items,
            'total': len(items),
            'available': len(usable),
            'providers': by_provider,
            'blocked_models': blocked_models,
            'using_fallback': not _keys,
            'gemini_quota_error': quota_error,
        }


def correct_gemini_usage(key_id, model, values):
    """Секрет выбирается на сервере; в браузер уходит только исправленная строка."""
    with _lock:
        entry = next((dict(k) for k in _keys if k['id'] == key_id and k['provider'] == 'gemini'), None)
    if not entry:
        raise ValueError('Ключ Gemini не найден в этом инстансе.')
    return gemini_quota.correct(entry['key'], model, values)


def key_values_for_copy(key_ids):
    """Секреты только явно выбранных ключей текущего профиля, не для статуса/SSE."""
    with _lock:
        available = {entry['id']: entry['key'] for entry in _keys}
        if any(identity not in available for identity in key_ids):
            raise ValueError('Один из ключей не найден в текущем инстансе.')
        return {identity: available[identity] for identity in key_ids}


def _model_blocked(st, model, now):
    if not model:
        return False
    entry = st.get('model_cooldowns', {}).get(model)
    return bool(entry and entry['until'] > now)


def _usable_order(model=None, provider='gemini'):
    """Ключи провайдера в порядке обхода, начиная с текущего курсора.

    Пропускаем ключи в кулдауне — целиком или для запрошенной модели.
    """
    now = time.time()
    ordered = []
    total = len(_keys)
    for offset in range(total):
        entry = _keys[(_cursor + offset) % total]
        if entry['provider'] != provider or not entry.get('enabled', True):
            continue
        st = _state[entry['id']]
        if st['cooldown_until'] > now or _model_blocked(st, model, now):
            continue
        ordered.append(entry)
    return ordered


def _all_busy_message(model, provider):
    """Текст ошибки, когда ни один ключ провайдера сейчас не годится."""
    now = time.time()
    label = providers.LABELS[provider]
    with _lock:
        mine = [k for k in _keys if k['provider'] == provider]
        enabled = [k for k in mine if k.get('enabled', True)]
        if not mine:
            return f"Нет ключей {label}. Добавьте их в разделе «Ключи API»."
        if not enabled:
            return f"Все ключи {label} выключены. Включите хотя бы один в настройках."
        # Для каждого ключа — что его держит: кулдаун целиком или лимит этой модели.
        blockers = []
        for k in enabled:
            st = _state[k['id']]
            if st['cooldown_until'] > now:
                blockers.append((st['cooldown_until'] - now, st['cooldown_reason'], False))
                continue
            mc = st.get('model_cooldowns', {}).get(model) if model else None
            if mc:
                blockers.append((mc['until'] - now, mc['reason'], True))
    if not blockers:
        return f"Ключи {label} сейчас недоступны."
    wait, reason, model_specific = min(blockers, key=lambda b: b[0])
    soonest = _fmt_wait(wait)
    if model_specific:
        return (f"Модель {model} сейчас недоступна на всех ключах {label} ({len(enabled)}): {reason}. "
                f"Ближайшая повторная попытка через {soonest} — или выберите другую модель.")
    return (f"Все ключи {label} ({len(enabled)}) на паузе: {reason}. "
            f"Ближайший освободится через {soonest}.")


# --------------------------------------------------------------------------
# Запасной клиент Gemini (когда пул пуст) — сохраняет старое поведение
# --------------------------------------------------------------------------

def _get_fallback_client():
    """Клиент Gemini из GOOGLE_API_KEY или Application Default Credentials."""
    global _fallback_client, _fallback_broken
    if instance_paths.IS_MANAGED:
        return None
    if _fallback_client is not None or _fallback_broken:
        return _fallback_client
    api_key = os.getenv('GOOGLE_API_KEY', '').strip()
    try:
        _fallback_client = genai.Client(api_key=api_key) if api_key else genai.Client()
    except Exception as e:
        logging.error(f"Не удалось создать запасной клиент Gemini: {e}")
        _fallback_broken = True
        _fallback_client = None
    return _fallback_client


def get_any_client(provider='gemini'):
    """Любой рабочий клиент провайдера — для служебных задач вроде списка моделей."""
    with _lock:
        for entry in _keys:
            if entry['provider'] == provider and entry.get('enabled', True):
                try:
                    return _get_client(entry)
                except Exception:
                    continue
    return _get_fallback_client() if provider == 'gemini' else None


def is_configured(provider=None):
    """Есть ли чем ходить в API (у провайдера или хоть у кого-то)."""
    with _lock:
        for k in _keys:
            if k.get('enabled', True) and (provider is None or k['provider'] == provider):
                return True
    if provider in (None, 'gemini'):
        return _get_fallback_client() is not None
    return False


# --------------------------------------------------------------------------
# Главная точка входа
# --------------------------------------------------------------------------

def call_with_rotation(fn, model=None, usage_fn=None, try_all_keys_on_error=False,
                       stop_event=None, progress=None, input_tokens_estimate=0, success_fn=None):
    """Вызывает fn(client), перебирая ключи провайдера при упирании в лимиты.

    Args:
        fn: функция одного аргумента (клиент провайдера), возвращающая ответ API.
        model: имя модели запроса — по нему выбирается провайдер, и на пару
            (ключ, модель) вешается кулдаун после 429.
        usage_fn: функция ответа → число токенов, для учёта дневного расхода.

    Returns:
        tuple: (результат | None, текст ошибки | None)
    """
    global _cursor

    provider = providers.provider_for_model(model)
    with _lock:
        has_pool = any(k['provider'] == provider for k in _keys)
        candidates = _usable_order(model, provider) if has_pool else []

    # Пул провайдера пуст — для Gemini работаем по-старому (ключ из окружения или ADC).
    if not has_pool:
        client = _get_fallback_client() if provider == 'gemini' else None
        if client is None:
            return None, _all_busy_message(model, provider)
        try:
            return fn(client), None
        except Exception as e:
            failure = _classify(e, provider, model)
            if failure is None:
                logging.error(f"Неожиданная ошибка при вызове {providers.LABELS[provider]}: {e}", exc_info=True)
                return None, str(e)
            return None, _describe_failure(failure, provider)

    if not candidates:
        return None, _all_busy_message(model, provider)

    errors_seen = []
    retry_delays = list(SERVER_ERROR_RETRY_DELAYS)
    for entry in candidates:
        key_id = entry['id']
        label = entry['label']
        tracked = provider == 'gemini' and gemini_quota.has_free_quota(
            gemini_quota.canonical_model(model))
        while True:
            if stop_event is not None and stop_event.is_set():
                return None, 'Генерация остановлена.'
            reservation = None
            if tracked:
                try:
                    reservation, quota_error = gemini_quota.reserve(entry['key'], model, input_tokens_estimate)
                except Exception as exc:
                    return None, f'Общий учёт Gemini недоступен: {exc}'
                if quota_error:
                    message = f'{label}: {quota_error}'
                    logging.info(message)
                    emit(progress, 'fallback', message, model=model)
                    errors_seen.append(message)
                    break
            emit(progress, 'request', f'Запрос {model}, ключ «{label}». Ожидаю ответ.', model=model)
            try:
                with _lock:
                    client = _get_client(entry)
                    if provider != 'gemini':
                        _record_request(key_id)
                result = fn(client)
            except Exception as e:
                failure = _classify(e, provider, model)
                if tracked:
                    if failure:
                        failure = dict(failure)
                        failure['reason'] = failure.get('reason', '').replace(entry['key'], '[КЛЮЧ]')
                    gemini_quota.finish(entry['key'], model, reservation, failure=failure)
                if failure is None:
                    description = str(e).replace(entry['key'], '[КЛЮЧ]')
                    with _lock:
                        _state[key_id]['fail_count'] += 1
                        _state[key_id]['last_error'] = description[:200]
                    logging.error('Ошибка %s на ключе %s: %s', providers.LABELS[provider], label, description)
                    if try_all_keys_on_error:
                        errors_seen.append(f'{label}: {description}')
                        emit(progress, 'fallback', f'{label}: {description}. Пробую следующий ключ.', model=model)
                        break
                    return None, description

                description = _describe_failure(failure, provider)
                if entry.get('key'):
                    description = description.replace(entry['key'], '[КЛЮЧ]')
                with _lock:
                    st = _state[key_id]
                    st['fail_count'] += 1
                    st['last_error'] = description[:2000]
                logging.warning("Ответ API на ключе '%s': %s", label, description)
                emit(progress, 'fallback', f'{label}: {description}', model=model)

                if failure['kind'] in ('quota', 'model_unavailable'):
                    target = failure['model']
                    with _lock:
                        if target and not tracked and failure['until'] > time.time():
                            st['model_cooldowns'][target] = {'until': failure['until'], 'reason': failure['reason']}
                        elif not target and failure['until'] > time.time():
                            st['cooldown_until'] = failure['until']
                            st['cooldown_reason'] = failure['reason']
                    scope = f" для {target}" if target else ''
                    logging.warning(f"Ключ '{label}'{scope}: {failure['reason']}. Беру следующий.")
                    errors_seen.append(f"{label}: {description}")
                    break

                if failure['kind'] == 'bad_key':
                    with _lock:
                        entry['enabled'] = False
                        st['cooldown_reason'] = 'ключ отклонён'
                    logging.error(f"Ключ '{label}' недействителен — выключаю его.")
                    errors_seen.append(f"{label}: {description}")
                    break

                if failure['kind'] == 'server':
                    if try_all_keys_on_error:
                        errors_seen.append(f'{label}: {description}')
                        break
                    if retry_delays:
                        wait = retry_delays.pop(0)
                        logging.warning(f"Сервер вернул {failure['code']} на ключе '{label}'. Повтор через {wait} с.")
                        if stop_event is not None:
                            if stop_event.wait(wait):
                                return None, 'Генерация остановлена.'
                        else:
                            time.sleep(wait)
                        continue
                    logging.warning(f"Сервер вернул {failure['code']} на ключе '{label}' и после повторов. Пробую следующий.")
                    errors_seen.append(f"{label}: {description}")
                    break

                # Неверный запрос или потерянный файл не исправляются сменой проекта.
                if try_all_keys_on_error:
                    errors_seen.append(f'{label}: {description}')
                    break
                return None, description

            else:
                try:
                    accepted = bool(success_fn(result)) if success_fn is not None else True
                except Exception:
                    if tracked:
                        gemini_quota.finish(entry['key'], model, reservation, success=False)
                    raise
                if tracked:
                    actual = getattr(getattr(result, 'usage_metadata', None), 'prompt_token_count', None)
                    gemini_quota.finish(entry['key'], model, reservation, success=accepted, input_tokens=actual)
                tokens = 0
                if usage_fn is not None:
                    try:
                        tokens = int(usage_fn(result) or 0)
                    except Exception:
                        tokens = 0
                with _lock:
                    if accepted:
                        _state[key_id]['success_count'] += 1
                        _state[key_id]['last_error'] = ''
                        if provider == 'gemini':
                            _record_request(key_id)
                    else:
                        _state[key_id]['fail_count'] += 1
                        _state[key_id]['last_error'] = 'API не вернул готового текстового ответа.'
                    _record_usage(key_id, model, tokens)
                    # Следующий запрос начнём со следующего ключа — равномернее нагрузка.
                    for idx, k in enumerate(_keys):
                        if k['id'] == key_id:
                            _cursor = (idx + 1) % len(_keys)
                            break
                return result, None

    detail = '; '.join(errors_seen) if errors_seen else 'причина неизвестна'
    return None, f"Все доступные ключи {providers.LABELS[provider]} ({len(candidates)}) не сработали. {detail}"
