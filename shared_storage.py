"""Атомарные JSON-записи и блокировки общих файлов между инстансами."""

from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_guard = threading.Lock()
_locks = {}
_held = threading.local()


class FileLease:
    """ОС освобождает блокировку при завершении процесса, даже после сбоя."""
    def __init__(self, path, timeout=10):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, 'a+b')
        self.file.seek(0, os.SEEK_END)
        if not self.file.tell():
            self.file.write(b'0'); self.file.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise TimeoutError('Файл занят другим инстансом.')
                time.sleep(.05)

    def close(self):
        if self.file.closed: return
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


@contextmanager
def file_lock(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, 'paths', None)
        if held is None: held = _held.paths = set()
        if key in held:
            yield
            return
        lease = FileLease(key + '.lock')
        held.add(key)
        try: yield
        finally:
            held.remove(key)
            lease.close()


def locked(paths):
    """Блокирует короткую операцию чтения/изменения; пути можно вычислить при вызове."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from contextlib import ExitStack
            values = paths() if callable(paths) else paths
            if isinstance(values, (str, Path)): values = [values]
            with ExitStack() as stack:
                for path in sorted(set(str(Path(value).resolve()) for value in values)):
                    stack.enter_context(file_lock(path))
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as file: return json.load(file)
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.flush(); os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
