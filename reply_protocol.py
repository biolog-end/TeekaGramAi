"""Разметка ответа с необязательной цитатой и именем; без зависимостей от Telegram."""
import re

ANSWER_PATTERN = (r'answer\s*'
                  r'(?:(?:\[(?P<quote>[^\]\r\n]*)\]|\\?<(?P<name>[^>\r\n]*)>)\s*){0,2}'
                  r'(?P<self>\(\s*ты\s*\))?\s*'
                  r'\\?\(\s*(?P<id>[^)\r\n]*)\s*\)')
ANSWER_RE = re.compile(ANSWER_PATTERN, re.IGNORECASE)
ANSWER_PLAIN_PATTERN = re.sub(r'\(\?P<\w+>', '(?:', ANSWER_PATTERN)
_HEADERS = re.compile(r'\(ID:\s*[^)\r\n]*\)|'
                      r'\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\]|'
                      r'<ник:[^>\r\n]*>', re.I)
_ORPHAN_REPLY_TAIL = re.compile(r'\]\\?<[^>\r\n]{1,120}>\s*\\?\(\s*\d+\s*\)')
_ORPHAN_REPLY_LINE = re.compile(
    r'(?m)^[^\r\n]{0,160}\]\\?<[^>\r\n]{1,120}>\s*\\?\(\s*\d+\s*\)')
_PARTIAL_ANSWER = re.compile(r'answer\s*(?:\[[^\]\r\n]*(?:\]|$)|\\?<[^>\r\n]*(?:>|$))', re.I)


def sanitize_outgoing_text(text):
    """Убирает служебную разметку даже посреди слова перед отправкой в Telegram."""
    clean = ANSWER_RE.sub('', text or '')
    clean = _PARTIAL_ANSWER.sub('', clean)
    clean = _ORPHAN_REPLY_LINE.sub('', clean)
    clean = _ORPHAN_REPLY_TAIL.sub('', clean)
    clean = _HEADERS.sub(' ', clean)
    return clean.strip()


def extract_reply(text):
    """Удалить все команды; цель берётся из первой. Скобки «(ты)» внутри имени безопасны."""
    matches = list(ANSWER_RE.finditer(text or ''))
    target = None
    if matches:
        first = matches[0]
        raw_id = first.group('id').strip()
        message_id = int(raw_id) if raw_id.isdecimal() and len(raw_id) <= 10 else None
        target = {'id': message_id if message_id and message_id <= 2_147_483_647 else None,
                  'quote': (first.group('quote') or '').strip(),
                  'name': (first.group('name') or '').strip()}
        if first.group('self') and '(ты)' not in target['name']:
            target['name'] += '(ты)'
    return sanitize_outgoing_text(text), target


def normalize_hint(value):
    return re.sub(r'\s+', ' ', (value or '').casefold().replace('ё', 'е')).strip()


def strip_system_lines(text):
    return _HEADERS.sub('', text or '').strip()


def split_outside_reply(text, separator):
    """{split} внутри цитаты — часть подсказки, а не граница сообщений."""
    ranges = [(m.start(), m.end()) for m in ANSWER_RE.finditer(text)]
    parts, last = [], 0
    for match in re.finditer(re.escape(separator), text):
        if any(start <= match.start() < end for start, end in ranges): continue
        parts.append(text[last:match.start()]); last = match.end()
    parts.append(text[last:])
    return parts


def quote_preview(value):
    text = re.sub(r'\s+', ' ', value or '').strip()
    short = ' '.join(text.split()[:8])[:64].rstrip()
    return short.replace(']', ')').replace('[', '(') + ('…' if len(short) < len(text) else '')
