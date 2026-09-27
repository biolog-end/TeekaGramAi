import re
from reply_protocol import ANSWER_PLAIN_PATTERN

# Служебный префикс, которым get_formatted_history размечает сообщения для модели:
# "(ID: 12) \n[2026-08-06 12:04:00]\n<ник:Соня> текст" — части разделены переносами.
# Перед ним могут стоять команды react()/answer(), их оставляем в тексте.
# Модели префикс нужен, а человеку в интерфейсе мешает — в вебе он показывается отдельно.
MESSAGE_PREFIX_RE = re.compile(
    r'^\s*(?P<lead>(?:(?:react\(\d+\)\[[^\]\n]*\]|' + ANSWER_PLAIN_PATTERN + r')\s*)*)'
    r'(?:\(ID:\s*(?P<id>\d+)\)\s*)?'
    r'(?:\[(?P<time>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\]\s*)?'
    r'(?:<ник:(?P<sender>[^>\n]*)>[ \t]*)?'
)


def split_message_prefix(text):
    """Отделяет служебный префикс от текста сообщения.

    Args:
        text (str): текст в том виде, в каком его видит модель.

    Returns:
        dict: {'id', 'time', 'sender', 'body'} — отсутствующие части равны None.
              Сам текст (body) возвращается без префикса.
    """
    if not text:
        return {'id': None, 'time': None, 'sender': None, 'body': ''}

    match = MESSAGE_PREFIX_RE.match(text)
    if not match or not match.end():
        return {'id': None, 'time': None, 'sender': None, 'body': text}

    time_value = match.group('time')
    if time_value:
        # В интерфейсе достаточно часов и минут, дата и так видна по порядку.
        parts = time_value.replace('T', ' ').split(' ')
        time_value = parts[1][:5] if len(parts) > 1 else time_value

    lead = (match.group('lead') or '').strip()
    body = text[match.end():]
    if lead:
        body = f"{lead}\n{body}" if body else lead

    return {
        'id': match.group('id'),
        'time': time_value,
        'sender': match.group('sender'),
        'body': body,
    }


# Реакции, которые принимает Telegram. Единый источник правды: этот список
# подставляется и в промпт персонажа, и в проверку ответа модели в bot_logic.
VALID_REACTIONS = [
    '👍', '❤️', '🔥', '🎉', '🤩', '😱', '😁', '😢', '🤔', '👎', '💩', '👌', '😈',
    '😨', '🕊', '🤬', '🤡', '😐', '🤝', '💯', '🥰', '🤮', '🦄', '😎', '💘', '👾',
]
from datetime import datetime
import logging
# Только через атрибут модуля: load_sticker_db() перепривязывает STICKER_DB на лету,
# а импорт по имени захватил бы старый словарь навсегда.
import telegram_utils

def parse_time_from_message(message_dict):
    """
    Вспомогательная функция для парсинга времени из текста сообщения.
    """
    try:
        if not message_dict or not isinstance(message_dict, dict) or "parts" not in message_dict:
             return None
        
        text_to_parse = None
        for part in message_dict.get("parts", []):
            if "text" in part and isinstance(part["text"], str):
                text_to_parse = part["text"]
                break 
        
        if text_to_parse is None:
            logging.warning("В сообщении не найдена текстовая часть для парсинга времени.")
            return None

        match = re.search(r"\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]", text_to_parse)
        
        
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        else:
            
            return None
            
    except Exception as e:
        logging.error(f"Ошибка парсинга времени из текста сообщения: {e}")
        return None


def split_message_by_limit(text: str, limit: int) -> list[str]:
    """
    Разделяет длинный текст на части, не превышающие заданный лимит.
    Разделение происходит по переносам строк, а затем по пробелам,
    чтобы не разрывать слова.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > 0:
        if len(text) <= limit:
            chunks.append(text)
            break
        
        split_pos = text.rfind('\n\n', 0, limit)
        if split_pos == -1:
            split_pos = text.rfind('\n', 0, limit)
        if split_pos == -1:
            split_pos = text.rfind(' ', 0, limit)

        if split_pos == -1:
            split_pos = limit

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip() 

    return chunks

def replace_standalone_sticker_names(text: str) -> str:
    """
    Находит "одинокие" кодовые имена стикеров в тексте и оборачивает их в команду sticker().
    Эта версия избегает ошибки "look-behind requires fixed-width pattern", разделяя
    текст на части и обрабатывая только те, что не являются командами sticker().
    """
    if not text or not re.search(r'[a-zA-Z]{3,}', text):
        return text

    # raw_* — стикеры на карантине без имени, их модель не должна «угадывать».
    sticker_codenames = sorted(
        (c for c in telegram_utils.STICKER_DB.keys() if not c.startswith('raw_')),
        key=len, reverse=True)
    if not sticker_codenames:
        return text

    # Не меняем image(...) и слова внутри цитаты/имени answer(...), даже при
    # совпадении со стикером. Иначе image(cat) превращался в image(sticker(cat)).
    sticker_command_pattern = re.compile(r'((?:sticker|image)\s*\([^)]+\)|' + ANSWER_PLAIN_PATTERN + ')', re.IGNORECASE)
    
    parts = sticker_command_pattern.split(text)
    
    result_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result_parts.append(part)
        else:
            processed_part = part
            for codename in sticker_codenames:
                simple_pattern = r'\b' + re.escape(codename) + r'\b'
                replacement = f'sticker({codename})'
                processed_part = re.sub(simple_pattern, replacement, processed_part, flags=re.IGNORECASE)
            result_parts.append(processed_part)

    return "".join(result_parts)
