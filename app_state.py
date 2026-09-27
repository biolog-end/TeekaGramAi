import threading

# Глобальные переменные для управления состоянием
telegram_thread = None
telegram_ready_event = threading.Event()

# Словарь для воркеров авто-режима
auto_mode_workers = {}
auto_mode_lock = threading.Lock()

gemini_client_global = None