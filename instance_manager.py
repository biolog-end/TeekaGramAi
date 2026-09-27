"""Дополнительные процессы одного проекта: аккаунты, отдельные ключи, запуск и остановка."""

import atexit
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid

import instance_paths as paths
from shared_storage import FileLease, file_lock, read_json, write_json

CONFIG_FILE = paths.ROOT / 'data' / 'instances.json'
ACCOUNTS_FILE = paths.ROOT / 'data' / 'accounts.json'
_processes = {}
_leases = []
_own = {'id': paths.INSTANCE_ID, 'name': 'Основной инстанс', 'port': 5001,
        'pid': os.getpid(), 'token': secrets.token_hex(32), 'session': '', 'account_name': ''}
_server = None


def runtime_file(instance_id):
    return paths.ROOT / 'data' / 'instances' / instance_id / 'runtime.json'


def profiles():
    return read_json(CONFIG_FILE, {'instances': []})['instances']


def profile(instance_id):
    return next((row for row in profiles() if row['id'] == instance_id), None)


def _session_path(session):
    value = Path(session)
    if not value.is_absolute(): value = paths.ROOT / value
    if value.suffix != '.session': value = Path(str(value) + '.session')
    return os.path.normcase(str(value.resolve()))


def _lease_path(kind, value):
    digest = hashlib.sha256(str(value).encode('utf-8')).hexdigest()
    return paths.ROOT / 'data' / 'instances' / 'locks' / f'{kind}_{digest}.lock'


def claim_session(session):
    try: _leases.append(FileLease(_lease_path('session', _session_path(session)), timeout=0))
    except TimeoutError: raise ValueError('Этот аккаунт уже запущен в другом инстансе.') from None


def claim_identity(user_id):
    try: _leases.append(FileLease(_lease_path('user', user_id), timeout=0))
    except TimeoutError: raise ValueError('Этот Telegram-аккаунт уже работает в другом инстансе.') from None


def _alive(pid):
    if not pid: return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle: return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally: kernel.CloseHandle(handle)
    try: os.kill(int(pid), 0); return True
    except OSError: return False


def _control(row, action='status'):
    request = Request(f'http://127.0.0.1:{row["port"]}/api/instance/control',
                      data=json.dumps({'action': action}).encode(),
                      headers={'Content-Type': 'application/json', 'X-Teeka-Control': row['token']})
    with urlopen(request, timeout=1.5) as response:
        result = json.load(response)
    if result.get('id') != row['id']: raise ValueError('Порт занят другим приложением.')
    return result


def _status(row):
    process = _processes.get(row['id'])
    runtime = read_json(runtime_file(row['id']), {})
    if process is not None and process.poll() is not None:
        return {'state': 'stopped', 'message': f'Процесс завершён (код {process.returncode}).'}
    if not _alive(runtime.get('pid')):
        return {'state': 'stopped', 'message': 'Остановлен.'}
    try: return _control(row)
    except (URLError, OSError, ValueError):
        return {'state': 'starting' if time.time() - runtime.get('started_at', 0) < 60 else 'unavailable',
                'message': 'Запускается…' if time.time() - runtime.get('started_at', 0) < 60 else 'Интерфейс пока недоступен. Проверьте журнал запуска.'}


def _public(row, status):
    keys_path = paths.ROOT / 'data' / 'instances' / row['id'] / 'api_keys.json'
    keys = read_json(keys_path, {'keys': []}).get('keys', [])
    return {'id': row['id'], 'name': row['name'], 'account_name': row['account_name'],
            'port': row['port'], 'url': f'http://127.0.0.1:{row["port"]}/',
            'keys': {provider: sum(1 for key in keys if key.get('provider', 'gemini') == provider and key.get('enabled', True))
                     for provider in ('gemini', 'openai')},
            'log': f'logs/instance_{row["id"]}.console.log',
            'state': status.get('state'), 'message': status.get('message', '')}


def list_instances():
    rows = profiles()
    if rows:
        with ThreadPoolExecutor(max_workers=min(16, len(rows))) as executor:
            states = list(executor.map(_status, rows))
    else: states = []
    accounts = read_json(ACCOUNTS_FILE, {})
    return {'instances': [_public(row, status) for row, status in zip(rows, states)],
            'accounts': list(accounts), 'main': {'name': _own['name'], 'account_name': _own['account_name'],
                'url': f'http://127.0.0.1:{_own["port"]}/', 'state': 'ready'}}


def _free_port(port):
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1', port)); return True
        except OSError: return False


def create(name, account_name='', new_account_name='', gemini_keys='', openai_keys=''):
    import re
    name = str(name).strip()
    if not name: raise ValueError('Введите название инстанса.')
    instance_id = uuid.uuid4().hex
    with file_lock(CONFIG_FILE), file_lock(ACCOUNTS_FILE):
        data = read_json(CONFIG_FILE, {'instances': []})
        accounts = read_json(ACCOUNTS_FILE, {})
        if account_name:
            if account_name not in accounts: raise ValueError('Telegram-аккаунт не найден.')
            session = accounts[account_name]
        else:
            account_name = str(new_account_name or name).strip()
            if account_name in accounts: raise ValueError('Аккаунт с этим названием уже существует. Выберите его из списка.')
            session = f'accounts/instance_{instance_id}'
            accounts[account_name] = session
        if _own['session'] and _session_path(session) == _session_path(_own['session']):
            raise ValueError('Этот аккаунт используется основным инстансом. Выберите другой.')
        if any(_session_path(row['session']) == _session_path(session) for row in data['instances']):
            raise ValueError('Для этого аккаунта уже создан инстанс.')
        keys = []
        for provider, text in [('gemini', gemini_keys), ('openai', openai_keys)]:
            for key in dict.fromkeys(re.split(r'[,;\s]+', str(text).strip())):
                if key: keys.append({'id': uuid.uuid4().hex, 'provider': provider, 'key': key, 'label': provider, 'enabled': True})
        used_ports = {row['port'] for row in data['instances']} | {_own['port']}
        port = next((port for port in range(max(5001, _own['port'] + 1), 65536) if port not in used_ports and _free_port(port)), None)
        if port is None: raise ValueError('Не найден свободный порт для инстанса.')
        number = max([int(os.getenv('INSTANCE_NUMBER', 1))] + [row['number'] for row in data['instances']]) + 1
        row = {'id': instance_id, 'name': name, 'account_name': account_name, 'session': session,
               'port': port, 'number': number, 'token': secrets.token_hex(32)}
        write_json(paths.ROOT / 'data' / 'instances' / instance_id / 'api_keys.json', {'keys': keys})
        write_json(ACCOUNTS_FILE, accounts)
        data['instances'].append(row)
        write_json(CONFIG_FILE, data)
    return _public(row, {'state': 'stopped', 'message': 'Создан. Можно запускать.'})


def start(instance_id):
    with file_lock(CONFIG_FILE), file_lock(runtime_file(instance_id)):
        row = profile(instance_id)
        if row is None: raise ValueError('Инстанс не найден.')
        if _status(row)['state'] != 'stopped': raise ValueError('Инстанс уже запущен.')
        try: probe = FileLease(_lease_path('session', _session_path(row['session'])), timeout=0)
        except TimeoutError: raise ValueError('Этот аккаунт уже запущен в другом инстансе.') from None
        probe.close()
        if not _free_port(row['port']): raise ValueError('Порт инстанса занят. Освободите его и повторите запуск.')
        env = dict(os.environ, TEEKA_INSTANCE_ID=row['id'], INSTANCE_NUMBER=str(row['number']),
                   TEEKA_MANAGER_PORT=str(_own['port']), PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        log_path = paths.ROOT / 'logs' / f'instance_{row["id"]}.console.log'
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, 'ab') as output:
            process = subprocess.Popen([sys.executable, str(paths.ROOT / 'main.py'), '--instance', row['id'],
                '--port', str(row['port']), '--no-browser'], cwd=paths.ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        _processes[row['id']] = process
        write_json(runtime_file(row['id']), {'pid': process.pid, 'started_at': time.time()})
        return _public(row, {'state': 'starting', 'message': 'Запускается…'})


def stop(instance_id):
    with file_lock(runtime_file(instance_id)):
        return _stop(instance_id)


def _stop(instance_id):
    row = profile(instance_id)
    if row is None: raise ValueError('Инстанс не найден.')
    process = _processes.get(instance_id)
    try: _control(row, 'stop')
    except (URLError, OSError, ValueError):
        if process is not None and process.poll() is None:
            process.terminate()  # Только дескриптор процесса, запущенного этим менеджером.
        elif _status(row)['state'] != 'stopped':
            raise ValueError('Инстанс не отвечает на остановку. Проверьте его журнал запуска.') from None
    if process is not None:
        try: process.wait(timeout=35)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
    return {'state': 'stopped', 'message': 'Инстанс остановлен.'}


def delete(instance_id):
    with file_lock(CONFIG_FILE), file_lock(runtime_file(instance_id)):
        row = profile(instance_id)
        if row is None: raise ValueError('Инстанс не найден.')
        if _status(row)['state'] != 'stopped': raise ValueError('Сначала остановите инстанс.')
        data = read_json(CONFIG_FILE, {'instances': []})
        data['instances'] = [row for row in data['instances'] if row['id'] != instance_id]
        write_json(CONFIG_FILE, data)
    # Аккаунт, его сессия и настройки сохраняются; общие данные не удаляются.


def initialize(port, session, name):
    if paths.IS_MANAGED:
        row = profile(paths.INSTANCE_ID)
        if row is None: raise ValueError('Профиль инстанса не найден.')
        _own.update(row)
    else:
        _own.update(port=port, session=session, account_name=name)
        # После сбоя основного окна живые дочерние процессы снова доступны менеджеру.
        for existing in profiles():
            runtime = read_json(runtime_file(existing['id']), {})
            if _alive(runtime.get('pid')):
                _processes.setdefault(existing['id'], None)
    _own['pid'] = os.getpid()
    claim_session(session)
    write_json(runtime_file(paths.INSTANCE_ID), {'pid': os.getpid(), 'started_at': time.time()})
    atexit.register(shutdown)


def own_snapshot():
    import instance_auth
    import telegram_utils
    state = instance_auth.snapshot()
    connected = telegram_utils.my_id is not None and telegram_utils.client and telegram_utils.client.is_connected()
    return {'id': paths.INSTANCE_ID, 'state': 'ready' if connected else 'login' if state['stage'] in ('phone', 'code', 'password', 'checking') else state['stage'],
            'message': 'Работает.' if connected else state['message']}


def set_server(server):
    global _server
    _server = server


def control(token, action):
    if not secrets.compare_digest(str(token or ''), _own['token']): raise PermissionError('Нет доступа.')
    result = own_snapshot()
    if action == 'stop' and _server is not None:
        threading.Thread(target=_server.shutdown, daemon=True).start()
    return result


def shutdown(parallel=False):
    try:
        if parallel and _processes:
            with ThreadPoolExecutor(max_workers=min(16, len(_processes))) as executor:
                list(executor.map(_quiet_stop, list(_processes)))
        else:
            # В atexit создавать ThreadPoolExecutor уже нельзя.
            for instance_id in list(_processes): _quiet_stop(instance_id)
    finally:
        _processes.clear()
        for lease in _leases: lease.close()
        _leases.clear()


def _quiet_stop(instance_id):
    try: stop(instance_id)
    except Exception: pass
