"""AI-описание стикеров: пачками, из истории чата.

Описанные стикеры получают codename и сразу появляются в списке «Активные» — включать
их персонажу или нет, решает пользователь галочкой. Карантин описатель не трогает:
карантинные стикеры модель картинками не получает, их описывают руками в модалке.
Если модель отказалась описывать пачку — вся пачка уходит в карантин, как и при
отказе в обычной генерации.

Модель просят прислать JSON — так проще разбирать, чем текст. Если она вернёт что-то
не то, мы это увидим в logs/sticker_ai.log; такой стикер просто останется без описания.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import time

import gemini_utils
import providers
import sticker_guard
import sticker_store
from shared_storage import locked, read_json, write_json

STICKER_AI_LOG = "logs/sticker_ai.log"
STICKER_AI_SETTINGS_FILE = "data/sticker_ai_settings.json"

# По одному запросу — не больше, иначе легко улететь в квоту 429 и в лимит контекста.
BATCH_SIZE = 12
DISCOVERY_TTL_SECONDS = 60 * 60
_discovery_cache = {}

DEFAULT_DESCRIBER_SYSTEM_PROMPT = """
Ты — оценщик и каталогизатор стикеров TeekaGramAi.

Как стикеры устроены в программе:
- команда sticker(codename) выбирает случайный Telegram-стикер из записи codename;
- у одного codename может быть несколько разных sticker_id — это варианты одной реакции для разнообразия;
- «пак» программы объединяет близкие codenames общим префиксом и имеет собственное описание;
- команды ручного скрипта: набор(pack_name) создаёт пак, описание(pack_name) задаёт его контекст,
  а пара «медиа-стикер → codename» добавляет вариант в запись;
- sticker_id и sticker_set_id после каждого медиа — реальные идентификаторы Telegram, не выдумывай их;
- telegram_pack_title / telegram_pack_short_name — название и короткая ссылка набора в Telegram,
  emoji — эмодзи, привязанный к конкретному стикеру.

Тебе будут даны каталог существующих паков, эталонные примеры и новые стикеры.
Эталонные примеры придут следующими отдельными сообщениями: сначала настоящее медиа стикера,
следом его codename, описание, sticker_id, sticker_set_id, название Telegram-пака и emoji.
Оценивай именно медиа, а не только подпись.
Для каждого НОВОГО стикера выбери короткий codename на латинице в snake_case. Если несколько новых
стикеров выражают одно и то же и подходят для случайного выбора, дай им одинаковый codename.
Старайся продолжать существующие названия и паки. Описание пиши по-русски кратко; если пользователь
в этом системном тексте попросил не писать описания, оставляй их пустыми.

Отвечай строго валидным JSON без Markdown и пояснений:
{"items":[{"index":1,"codename":"menhera_chan_happy","description":"радостно улыбается",
"pack_name":"menhera_chan","pack_description":"контекст всего пака"}]}
pack_name и pack_description можно оставить пустыми. Возвращай только индексы новых стикеров.
""".strip()


def get_system_prompt():
    data = read_json(STICKER_AI_SETTINGS_FILE, {}) or {}
    value = data.get('system_prompt')
    return value if isinstance(value, str) and value.strip() else DEFAULT_DESCRIBER_SYSTEM_PROMPT


@locked(lambda: STICKER_AI_SETTINGS_FILE)
def save_system_prompt(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Системный текст описателя не может быть пустым.')
    write_json(STICKER_AI_SETTINGS_FILE, {'system_prompt': value})
    return value


@locked(lambda: STICKER_AI_SETTINGS_FILE)
def reset_system_prompt():
    write_json(STICKER_AI_SETTINGS_FILE, {'system_prompt': DEFAULT_DESCRIBER_SYSTEM_PROMPT})
    return DEFAULT_DESCRIBER_SYSTEM_PROMPT


def _log(msg):
    try:
        os.makedirs('logs', exist_ok=True)
        with open(STICKER_AI_LOG, 'a', encoding='utf-8') as f:
            f.write(msg + "\n")
    except IOError:
        pass


def _parse_json(text):
    """Модели любят обернуть JSON в ```json``` — вычленяем его сами."""
    if not text:
        return None
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        _log(f"JSON parse error: {e}\nraw: {text[:1000]}")
        return None


def _pick_model(chat_model_name):
    """Для описания стикеров нужна vision-модель. У Gemini это flash-lite (дёшево);
    OpenAI mini/nano тоже понимают картинки, но платно — берём Gemini как дефолт."""
    if providers.provider_for_model(chat_model_name) == 'gemini':
        return chat_model_name or providers.default_model('gemini')
    import key_pool
    if key_pool.is_configured('gemini'):
        return providers.default_model('gemini')
    return chat_model_name


def _pack_name(codename, data):
    candidates = [name for name, record in data.items()
                  if not record.get('stickers') and (codename == name or codename.startswith(name + '_'))]
    return max(candidates, key=len) if candidates else 'остальные'


def _catalog_text(data):
    """Полный справочник проекта: паки, описания, варианты и реальные Telegram id."""
    lines = ['СПРАВОЧНИК СУЩЕСТВУЮЩИХ СТИКЕРОВ ПРОЕКТА:']
    pack_names = sorted(name for name, record in data.items()
                        if not record.get('stickers') and not name.startswith(sticker_store.QUARANTINE_PREFIX))
    for name in pack_names:
        lines.append(f"Пак программы **{name}** — {data[name].get('description') or 'без описания'}")
    has_other = any(record.get('stickers') and _pack_name(name, data) == 'остальные'
                    for name, record in data.items())
    if has_other:
        lines.append('Пак программы **остальные** — стикеры без отдельного объявленного пака.')
    for codename, record in sorted(data.items()):
        if codename.startswith(sticker_store.QUARANTINE_PREFIX) or not record.get('stickers'):
            continue
        ids = [str(item.get('id')) for item in record['stickers'] if item.get('id') is not None]
        set_ids = sorted({str(item.get('set_id')) for item in record['stickers'] if item.get('set_id')})
        set_titles = sorted({str(item.get('set_title')) for item in record['stickers'] if item.get('set_title')})
        set_short_names = sorted({str(item.get('set_short_name')) for item in record['stickers']
                                  if item.get('set_short_name')})
        emojis = sorted({str(item.get('emoji')) for item in record['stickers'] if item.get('emoji')})
        lines.append(f"- pack={_pack_name(codename, data)}; codename={codename}; "
                     f"description={record.get('description') or '—'}; sticker_ids={','.join(ids) or '—'}; "
                     f"telegram_set_ids={','.join(set_ids) or '—'}; "
                     f"telegram_pack_titles={','.join(set_titles) or '—'}; "
                     f"telegram_pack_short_names={','.join(set_short_names) or '—'}; "
                     f"emojis={''.join(emojis) or '—'}")
    return '\n'.join(lines)


def _reference_entries(data, other_count=9, selected_ids=None):
    """Все варианты menhera_chan и девять случайных примеров из остальных."""
    menhera, other = [], []
    for codename, sticker in sticker_store.all_stickers(data):
        if codename.startswith(sticker_store.QUARANTINE_PREFIX) or sticker.get('quarantined'):
            continue
        entry = {**sticker, 'codename': codename, 'description': data[codename].get('description', ''),
                 'pack_name': _pack_name(codename, data)}
        if codename == 'menhera_chan' or codename.startswith('menhera_chan_'):
            menhera.append(entry)
        elif entry['pack_name'] == 'остальные':
            other.append(entry)
    if selected_ids is None:
        chosen = random.sample(other, min(other_count, len(other)))
    else:
        wanted = set()
        for value in selected_ids:
            try:
                wanted.add(int(value))
            except (TypeError, ValueError):
                continue
        chosen = [entry for entry in other if int(entry['id']) in wanted][:other_count]
    return menhera + chosen


def reference_preview(data=None):
    """Метаданные точных эталонов для показа в UI и повторной передачи по ID."""
    data = sticker_store.load() if data is None else data
    return [{key: entry.get(key) for key in
             ('id', 'codename', 'description', 'pack_name', 'set_id', 'set_title',
              'set_short_name', 'emoji')}
            for entry in _reference_entries(data)]


def _telegram_metadata_text(entry):
    """Одинаковая подпись к эталонному и новому медиа для модели."""
    return (f"sticker_id={entry['id']}; "
            f"telegram_sticker_set_id={entry.get('set_id') or 'неизвестен'}; "
            f"telegram_pack_title={entry.get('set_title') or 'неизвестно'}; "
            f"telegram_pack_short_name={entry.get('set_short_name') or 'неизвестно'}; "
            f"emoji={entry.get('emoji') or 'неизвестно'}")


async def _media_part(entry, telegram_utils_module):
    """Медиа стикера для модели: WebM как анимированный GIF, прочее как JPEG."""
    message = entry.get('message') or entry.get('media_source')
    if message:
        # Карточке UI всё равно нужен статичный JPEG thumbnail. Для модели общий
        # helper ниже отдельно отдаст полную GIF-анимацию WebM-стикера.
        await telegram_utils_module.get_sticker_preview(
            entry['id'], entry.get('access_hash'), message=message)
    media_path, media_mime = await telegram_utils_module.get_sticker_model_media(
        entry['id'], entry.get('access_hash'), message=message)
    if not media_path:
        return None
    try:
        with open(media_path, 'rb') as stream:
            raw = stream.read()
        return {'mime_type': media_mime, 'image_base64': base64.b64encode(raw).decode('ascii'),
                'sticker_id': entry['id']}
    except IOError as exc:
        _log(f"[media-error] sticker_id={entry['id']}: {exc}")
        return None


async def _reference_history(data, telegram_utils_module, source_messages=None, selected_ids=None):
    history = []
    for entry in _reference_entries(data, selected_ids=selected_ids):
        entry = dict(entry)
        if source_messages and entry['id'] in source_messages:
            entry['message'] = source_messages[entry['id']]
        media = await _media_part(entry, telegram_utils_module)
        if not media:
            continue
        history.append({'role': 'user', 'parts': [media, {'text':
            f"ЭТАЛОН: codename={entry['codename']}; pack={entry['pack_name']}; "
            f"description={entry.get('description') or '—'}; {_telegram_metadata_text(entry)}"}]})
    return history


async def _describe_batch(batch, model_name, telegram_utils_module, system_prompt,
                          catalog_text, reference_history):
    """Один вызов модели: batch = список {id, access_hash, index}.

    Возвращает (dict id → {codename, description, pack}, refused): refused=True —
    модель ответила отказом, пачку надо в карантин.
    """
    history = list(reference_history)
    submitted = 0
    for entry in batch:
        media = await _media_part(entry, telegram_utils_module)
        if not media:
            _log(f"[skip] нет превью для {entry['id']}")
            continue
        submitted += 1
        history.append({'role': 'user', 'parts': [media, {'text':
            f"НОВЫЙ СТИКЕР index={entry['index']}; {_telegram_metadata_text(entry)}"}]})

    if not submitted:
        return {}, False, {'submitted': 0, 'requested': False, 'requests': 0, 'attempts': 0}
    # Мы внутри цикла Telethon: синхронный HTTP к модели заблокировал бы его на секунды
    # каждого батча, а Telegram за это время может отвалиться. Уводим в поток.
    loop = asyncio.get_running_loop()
    async def request(request_history):
        return await loop.run_in_executor(
            None,
            lambda: gemini_utils.generate_chat_reply_original(
                model_name=model_name,
                system_prompt=system_prompt + "\n\n" + catalog_text,
                chat_history=request_history))

    attempts = 1
    text, err = await request(history)

    if err:
        _log(f"[error] {model_name}: {err}")
        return {}, sticker_guard.looks_like_refusal(None, err), {
            'submitted': submitted, 'requested': True, 'requests': 0, 'attempts': attempts,
            'error': err}

    _log(f"[batch] model={model_name}, size={len(batch)}, raw={text[:2000]}")
    if sticker_guard.looks_like_refusal(text):
        return {}, True, {'submitted': submitted, 'requested': True, 'requests': 1,
                          'attempts': attempts}
    data = _parse_json(text or '')
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        return {}, False, {'submitted': submitted, 'requested': True, 'requests': 1,
                           'attempts': attempts}

    by_index = {int(entry['index']): entry for entry in batch}
    result = {}
    for item in data['items']:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get('index'))
        except (TypeError, ValueError):
            continue
        source = by_index.get(idx)
        if not source:
            continue
        codename = re.sub(r'[^a-z0-9_]+', '_', str(item.get('codename') or '').strip().lower()).strip('_')
        if not codename:
            continue
        result[source['id']] = {
            'codename': codename,
            'description': str(item.get('description') or '').strip(),
            'pack_name': re.sub(r'[^a-z0-9_]+', '_', str(item.get('pack_name') or '').strip().lower()).strip('_'),
            'pack_description': str(item.get('pack_description') or '').strip(),
        }
    return result, False, {'submitted': submitted, 'requested': True, 'requests': 1,
                           'attempts': attempts}


def _set_key(entry):
    reference = entry.get('set_reference')
    set_id = entry.get('set_id') or getattr(reference, 'id', None)
    short_name = entry.get('set_short_name') or getattr(reference, 'short_name', None)
    if set_id:
        return 'id', int(set_id)
    if short_name:
        return 'short_name', str(short_name)
    return None


async def _enrich_pack_metadata(entries, telegram_utils_module):
    """Дополняет название набора и emoji одним запросом на каждый Telegram-пак."""
    try:
        from telethon.tl.functions.messages import GetStickerSetRequest
    except ImportError:
        return

    grouped = {}
    for entry in entries:
        key = _set_key(entry)
        reference = entry.get('set_reference')
        if key and reference is not None:
            grouped.setdefault(key, {'reference': reference, 'entries': []})['entries'].append(entry)

    for group in grouped.values():
        try:
            result = await telegram_utils_module.client(
                GetStickerSetRequest(stickerset=group['reference'], hash=0))
        except Exception as exc:
            _log(f"[sticker-set-error] {_set_key(group['entries'][0])}: {exc}")
            continue
        sticker_set = getattr(result, 'set', None)
        emoji_by_id = {}
        documents_by_id = {int(document.id): document
                           for document in (getattr(result, 'documents', None) or [])}
        for pack in getattr(result, 'packs', None) or []:
            for sticker_id in getattr(pack, 'documents', None) or []:
                emoji_by_id.setdefault(int(sticker_id), getattr(pack, 'emoticon', '') or '')
        for document in getattr(result, 'documents', None) or []:
            meta = sticker_store.telegram_sticker_meta(document)
            if meta.get('emoji'):
                emoji_by_id[int(document.id)] = meta['emoji']
        for entry in group['entries']:
            entry['set_id'] = getattr(sticker_set, 'id', None) or entry.get('set_id')
            entry['set_access_hash'] = (getattr(sticker_set, 'access_hash', None)
                                        or entry.get('set_access_hash'))
            entry['set_title'] = getattr(sticker_set, 'title', '') or entry.get('set_title', '')
            entry['set_short_name'] = (getattr(sticker_set, 'short_name', '')
                                       or entry.get('set_short_name', ''))
            entry['emoji'] = entry.get('emoji') or emoji_by_id.get(int(entry['id']), '')
            if int(entry['id']) in documents_by_id:
                entry['media_source'] = documents_by_id[int(entry['id'])]


async def _refresh_reference_metadata(store_data, telegram_utils_module, selected_ids=None):
    """Перед AI-вызовом дополняет метаданные именно показанных эталонов."""
    references = _reference_entries(store_data, selected_ids=selected_ids)
    try:
        from telethon.tl.types import InputStickerSetID
    except ImportError:
        return store_data, [entry['id'] for entry in references], {}

    targets = []
    for entry in references:
        if entry.get('set_id') and entry.get('set_access_hash'):
            entry = dict(entry)
            entry['set_reference'] = InputStickerSetID(
                id=int(entry['set_id']), access_hash=int(entry['set_access_hash']))
            targets.append(entry)
    await _enrich_pack_metadata(targets, telegram_utils_module)
    media_sources = {int(entry['id']): entry['media_source'] for entry in targets
                     if entry.get('media_source') is not None}
    for entry in targets:
        sticker_store.ingest_unknown(
            entry['id'], entry.get('access_hash'), entry.get('set_id'),
            entry.get('set_access_hash'), entry.get('set_title', ''),
            entry.get('set_short_name', ''), entry.get('emoji', ''))
    return sticker_store.load(), [entry['id'] for entry in references], media_sources


async def _collect_unknown_from_chat(chat_id, limit, telegram_utils_module):
    """Сканирует чат и возвращает безымянные стикеры с полными метаданными."""
    if not telegram_utils_module.client or not telegram_utils_module.client.is_connected():
        return None, None, None, "Telegram client not connected."
    try:
        messages = await telegram_utils_module.client.get_messages(chat_id, limit=limit)
    except Exception as exc:
        return None, None, None, f"Ошибка чтения истории: {exc}"

    stored_before = sticker_store.load()
    observed = []
    source_messages = {}
    seen = set()
    for message in messages or []:
        sticker = getattr(message, 'sticker', None)
        if not sticker:
            continue
        source_messages[sticker.id] = message
        if sticker.id in seen:
            continue
        seen.add(sticker.id)
        meta = sticker_store.telegram_sticker_meta(sticker)
        _, existing = sticker_store.find_by_id(sticker.id, stored_before)
        observed.append({
            'id': sticker.id,
            'access_hash': sticker.access_hash,
            'message': message,
            **meta,
            'set_title': meta.get('set_title') or (existing or {}).get('set_title', ''),
            'set_short_name': meta.get('set_short_name') or (existing or {}).get('set_short_name', ''),
            'emoji': meta.get('emoji') or (existing or {}).get('emoji', ''),
        })

    await _enrich_pack_metadata(observed, telegram_utils_module)
    for entry in observed:
        sticker_store.ingest_unknown(
            entry['id'], entry.get('access_hash'), entry.get('set_id'),
            entry.get('set_access_hash'), entry.get('set_title', ''),
            entry.get('set_short_name', ''), entry.get('emoji', ''))

    store_data = sticker_store.load()
    telegram_utils_module.load_sticker_db()
    unknown = []
    for entry in observed:
        codename, stored = sticker_store.find_by_id(entry['id'], store_data)
        if not stored or stored.get('quarantined'):
            continue
        if not codename.startswith(sticker_store.QUARANTINE_PREFIX):
            continue
        unknown.append({**entry,
                        'set_id': stored.get('set_id') or entry.get('set_id'),
                        'set_access_hash': stored.get('set_access_hash') or entry.get('set_access_hash'),
                        'set_title': stored.get('set_title') or entry.get('set_title', ''),
                        'set_short_name': stored.get('set_short_name') or entry.get('set_short_name', ''),
                        'emoji': stored.get('emoji') or entry.get('emoji', '')})
    return unknown, store_data, source_messages, None


def _public_candidate(entry, media_available):
    return {
        # Строки сохраняют 64-битный Telegram ID без округления в JavaScript.
        'id': str(entry['id']),
        'set_id': str(entry.get('set_id')) if entry.get('set_id') is not None else '',
        'set_title': entry.get('set_title', ''),
        'set_short_name': entry.get('set_short_name', ''),
        'emoji': entry.get('emoji', ''),
        'media_available': bool(media_available),
    }


async def find_stickers_in_chat(chat_id, limit=100):
    """Первый шаг UI: находит и заранее загружает медиа без обращения к модели."""
    import telegram_utils
    unknown, _, _, error = await _collect_unknown_from_chat(
        chat_id, limit, telegram_utils)
    if error:
        return None, error
    candidates = []
    cached_entries = {}
    for entry in unknown:
        media = await _media_part(entry, telegram_utils)
        if media is not None:
            cached_entries[int(entry['id'])] = entry
        candidates.append(_public_candidate(entry, media is not None))
    now = time.monotonic()
    for cached_chat, snapshot in list(_discovery_cache.items()):
        if now - snapshot.get('created_at', 0) > DISCOVERY_TTL_SECONDS:
            _discovery_cache.pop(cached_chat, None)
    _discovery_cache[int(chat_id)] = {
        'created_at': now,
        'limit': limit,
        'entries': cached_entries,
    }
    return {
        'found': len(candidates),
        'media_ready': sum(1 for item in candidates if item['media_available']),
        'candidates': candidates,
    }, None


def _apply_descriptions(described, source_by_id=None):
    """Дописывает codename/description и возвращает точный отчёт для интерфейса."""
    source_by_id = source_by_id or {}
    before = sticker_store.load()
    existing_packs = {name for name, record in before.items() if not record.get('stickers')}
    stickers_added = []
    packs_written = {}
    for sticker_id, info in described.items():
        pack_name = info.get('pack_name', '')
        codename = info['codename']
        if pack_name and codename != pack_name and not codename.startswith(pack_name + '_'):
            codename = f"{pack_name}_{codename}"
        ok, saved_codename = sticker_store.describe(sticker_id, codename, info['description'])
        if not ok:
            continue

        source = source_by_id.get(int(sticker_id), {})
        stickers_added.append({
            'sticker_id': str(sticker_id),
            'codename': saved_codename,
            'description': info.get('description', ''),
            'program_pack': pack_name or 'остальные',
            'telegram_set_id': (str(source.get('set_id')) if source.get('set_id') is not None else ''),
            'telegram_pack_title': source.get('set_title', ''),
            'telegram_pack_short_name': source.get('set_short_name', ''),
            'emoji': source.get('emoji', ''),
        })

        if pack_name and pack_name not in packs_written:
            sticker_store.describe_set(pack_name, info.get('pack_description', ''))
            packs_written[pack_name] = {
                'name': pack_name,
                'description': info.get('pack_description', ''),
                'created': pack_name not in existing_packs,
            }
    return {
        'applied': len(stickers_added),
        'stickers': stickers_added,
        'packs': list(packs_written.values()),
    }


async def describe_from_chat(chat_id, chat_model_name, limit=100, system_prompt=None,
                             reference_ids=None, candidate_ids=None):
    """Второй шаг UI: описывает только выбранные после поиска стикеры."""
    import telegram_utils   # локальный импорт — telegram_utils сам подтягивает store
    wanted = []
    for value in candidate_ids or []:
        try:
            sticker_id = int(value)
        except (TypeError, ValueError):
            continue
        if sticker_id not in wanted:
            wanted.append(sticker_id)
    if not wanted:
        return None, "Список найденных стикеров пуст. Нажмите «Найти стикеры» ещё раз."

    snapshot = _discovery_cache.get(int(chat_id), {})
    if time.monotonic() - snapshot.get('created_at', 0) <= DISCOVERY_TTL_SECONDS:
        cached = snapshot.get('entries', {})
    else:
        cached = {}
    unknown = [dict(cached[sticker_id]) for sticker_id in wanted if sticker_id in cached]

    # Запасной путь нужен после перезапуска процесса или если часть карточек исчезла
    # из часового снимка. Он дополняет, а не заменяет уже загруженные Message-объекты.
    missing = set(wanted) - {int(entry['id']) for entry in unknown}
    store_data = sticker_store.load()
    source_messages = {int(entry['id']): entry.get('message') for entry in unknown
                       if entry.get('message') is not None}
    if missing:
        rescanned, store_data, rescanned_messages, error = await _collect_unknown_from_chat(
            chat_id, limit, telegram_utils)
        if error:
            return None, error
        for entry in rescanned:
            if int(entry['id']) in missing:
                unknown.append(entry)
                missing.discard(int(entry['id']))
        source_messages.update(rescanned_messages)

    if missing:
        return None, ("Не удалось восстановить найденные стикеры: "
                      + ", ".join(str(value) for value in sorted(missing))
                      + ". Нажмите «Найти стикеры» ещё раз.")

    if not unknown:
        return None, "Найденные стикеры больше недоступны. Выполните поиск ещё раз."

    _log(f"\n=== describe_from_chat({chat_id}) — {len(unknown)} новых стикеров ===")
    model_name = _pick_model(chat_model_name)
    described_total = 0
    quarantined_total = 0
    requests_total = 0
    attempts_total = 0
    media_sent_total = 0
    added_stickers = []
    added_packs = {}
    errors = []
    store_data, effective_reference_ids, reference_sources = await _refresh_reference_metadata(
        store_data, telegram_utils, selected_ids=reference_ids)
    source_messages.update(reference_sources)
    catalog_text = _catalog_text(store_data)
    references = await _reference_history(
        store_data, telegram_utils, source_messages, selected_ids=effective_reference_ids)
    prompt = system_prompt if isinstance(system_prompt, str) and system_prompt.strip() else get_system_prompt()
    for start in range(0, len(unknown), BATCH_SIZE):
        batch = [{**entry, 'index': i + 1} for i, entry in enumerate(unknown[start:start + BATCH_SIZE])]
        described, refused, meta = await _describe_batch(
            batch, model_name, telegram_utils, prompt, catalog_text, references)
        requests_total += int(meta.get('requests', int(meta.get('requested', False))))
        attempts_total += int(meta.get('attempts', meta.get('requests',
                                                            int(meta.get('requested', False)))))
        media_sent_total += int(meta.get('submitted', 0))
        if meta.get('error'):
            errors.append(str(meta['error']))
        if refused:
            ids = [entry['id'] for entry in batch]
            quarantined_total += sticker_store.quarantine(ids)
            _log(f"[quarantine] отказ модели, в карантин: {ids}")
            continue
        applied = _apply_descriptions(described, {int(entry['id']): entry for entry in batch})
        described_total += applied['applied']
        added_stickers.extend(applied['stickers'])
        for pack in applied['packs']:
            previous = added_packs.get(pack['name'])
            if previous:
                previous['created'] = previous['created'] or pack['created']
                if pack.get('description'):
                    previous['description'] = pack['description']
            else:
                added_packs[pack['name']] = pack

    telegram_utils.load_sticker_db()
    return {'found': len(unknown), 'described': described_total,
            'quarantined': quarantined_total, 'model': model_name,
            'requests': requests_total, 'attempts': attempts_total, 'media_sent': media_sent_total,
            'reference_examples': len(references), 'errors': errors,
            'added_stickers': added_stickers, 'added_packs': list(added_packs.values())}, None
