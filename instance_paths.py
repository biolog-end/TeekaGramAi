"""Общие данные проекта и отдельные данные дополнительного инстанса."""

import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
INSTANCE_ID = os.getenv('TEEKA_INSTANCE_ID', 'main')
if INSTANCE_ID != 'main' and not re.fullmatch(r'[a-f0-9]{32}', INSTANCE_ID):
    raise ValueError('Неверный ID инстанса.')
IS_MANAGED = INSTANCE_ID != 'main'


def private_path(filename):
    if IS_MANAGED:
        return str(ROOT / 'data' / 'instances' / INSTANCE_ID / filename)
    return str(ROOT / 'data' / filename)


def manager_url():
    port = os.getenv('TEEKA_MANAGER_PORT', os.getenv('INSTANCE_NUMBER', '1'))
    if not os.getenv('TEEKA_MANAGER_PORT'): port = str(5000 + int(port))
    return f'http://127.0.0.1:{int(port)}/?instances=1'
