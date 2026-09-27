"""Меню инстансов и веб-вход дополнительных Telegram-аккаунтов."""

from flask import jsonify, redirect, render_template, request, url_for
from urllib.parse import urlsplit

import instance_auth
import instance_manager as manager
import instance_paths as paths


def _fail(message, status=400):
    return jsonify(status='error', message=str(message)), status


def access_check():
    protected = request.path.startswith('/api/instances') or request.path.startswith('/api/login') or request.path in {'/api/instance/control', '/api/keys/copy'}
    if protected and request.method == 'POST' and request.headers.get('Origin'):
        origin = urlsplit(request.headers['Origin'])
        if origin.netloc != request.host or origin.scheme != 'http':
            return _fail('Запрос должен приходить из интерфейса этого инстанса.', 403)
    if paths.IS_MANAGED and instance_auth.snapshot()['stage'] != 'ready':
        allowed = request.endpoint in {'instance_login', 'instance_login_status', 'instance_login_submit', 'instance_control', 'static'}
        if not allowed:
            if request.method == 'GET' and not request.path.startswith(('/api/', '/events/', '/media/')):
                return redirect(url_for('instance_login'))
            return _fail('Сначала войдите в Telegram-аккаунт этого инстанса.', 503)


def list_instances():
    if paths.IS_MANAGED: return _fail('Управление инстансами доступно в основном окне.', 403)
    return jsonify(status='success', **manager.list_instances())


def create_instance():
    if paths.IS_MANAGED: return _fail('Откройте меню в основном инстансе.', 403)
    body = request.get_json(silent=True) or {}
    try:
        row = manager.create(body.get('name', ''), body.get('account_name', ''),
                             body.get('new_account_name', ''), body.get('gemini_keys', ''), body.get('openai_keys', ''))
        return jsonify(status='success', message='Инстанс создан.', instance=row)
    except (ValueError, OSError, TimeoutError) as error: return _fail(error)


def instance_action(instance_id, action):
    if paths.IS_MANAGED: return _fail('Откройте меню в основном инстансе.', 403)
    try:
        if action == 'start': result = manager.start(instance_id)
        elif action == 'stop': result = manager.stop(instance_id)
        elif action == 'delete': manager.delete(instance_id); result = {}
        else: raise ValueError('Неизвестное действие.')
        return jsonify(status='success', instance=result)
    except (ValueError, OSError, TimeoutError) as error: return _fail(error)


def control():
    body = request.get_json(silent=True) or {}
    try: return jsonify(manager.control(request.headers.get('X-Teeka-Control'), body.get('action', 'status')))
    except PermissionError as error: return _fail(error, 403)


def login():
    if not paths.IS_MANAGED or instance_auth.snapshot()['stage'] == 'ready': return redirect(url_for('index'))
    return render_template('instance_login.html')


def login_status():
    return jsonify(status='success', **instance_auth.snapshot())


def login_submit():
    if not paths.IS_MANAGED: return _fail('Вход основного аккаунта выполняется при запуске.', 403)
    body = request.get_json(silent=True) or {}
    try:
        instance_auth.submit(body.get('challenge'), body.get('value', ''))
        return jsonify(status='success')
    except ValueError as error: return _fail(error)
