"""Буквальные замены лексикона персонажа перед симуляцией опечаток."""

import json
import math
import random
import re


def parse_lexicon_rules(value):
    """Проверяет упорядоченный список и возвращает независимые копии правил."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError('Лексикон персонажа: некорректный список замен.') from exc
    if not isinstance(value, list):
        raise ValueError('Лексикон персонажа должен содержать список замен.')
    rules = []
    for index, rule in enumerate(value, 1):
        if not isinstance(rule, dict):
            raise ValueError(f'Лексикон, строка {index}: некорректная замена.')
        source, replacement = rule.get('find'), rule.get('replace', '')
        if not isinstance(source, str) or not source:
            raise ValueError(f'Лексикон, строка {index}: укажите, что искать.')
        if not isinstance(replacement, str):
            raise ValueError(f'Лексикон, строка {index}: замена должна быть текстом.')
        raw_chance = rule.get('chance', 1)
        try:
            chance = float(raw_chance)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Лексикон, строка {index}: шанс должен быть от 0 до 1.') from exc
        if isinstance(raw_chance, bool) or not math.isfinite(chance) or not 0 <= chance <= 1:
            raise ValueError(f'Лексикон, строка {index}: шанс должен быть от 0 до 1.')
        rules.append({'find': source, 'replace': replacement, 'chance': chance})
    return rules


def apply_character_lexicon(text, rules, random_draw=None):
    """Сверху вниз; независимый шанс для каждого непересекающегося совпадения.

    Поиск учитывает регистр и любые символы. Вставленный текст участвует в следующих
    правилах, но не обрабатывается повторно внутри того же правила.
    """
    draw = random_draw or random.random
    for rule in parse_lexicon_rules(rules):
        source, replacement, chance = rule['find'], rule['replace'], rule['chance']
        if chance == 1:
            text = text.replace(source, replacement)
        elif chance > 0:
            text = re.sub(re.escape(source),
                          lambda match: replacement if draw() < chance else match.group(0), text)
    return text
