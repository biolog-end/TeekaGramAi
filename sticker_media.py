"""Подготовка анимированных Telegram-стикеров для vision-моделей."""

import logging
import os
import shutil
import subprocess


def ffmpeg_executable():
    """Находит FFmpeg: сначала пакет imageio-ffmpeg, затем системный PATH."""
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and os.path.isfile(executable):
            return executable
    except Exception as exc:
        logging.debug("imageio-ffmpeg недоступен: %s", exc)
    return shutil.which('ffmpeg')


def webm_to_gif(webm_bytes, *, max_side=512, fps=12, timeout=30):
    """Конвертирует WebM целиком в зацикленный анимированный GIF.

    Возвращает bytes или None. Ввод и вывод идут через pipe, поэтому исходный WebM
    не остаётся на диске. Палитра строится отдельно для каждого стикера.
    """
    if not webm_bytes:
        return None
    executable = ffmpeg_executable()
    if not executable:
        logging.warning("FFmpeg не найден: WebM-стикер нельзя преобразовать в GIF.")
        return None

    filter_graph = (
        f"[0:v]fps={int(fps)},"
        f"scale={int(max_side)}:{int(max_side)}:force_original_aspect_ratio=decrease:flags=lanczos,"
        "setsar=1,split[frames][palette_source];"
        "[palette_source]palettegen=max_colors=128:reserve_transparent=1[palette];"
        "[frames][palette]paletteuse=dither=bayer:bayer_scale=3:alpha_threshold=128[out]"
    )
    command = [
        executable, '-hide_banner', '-loglevel', 'error',
        '-i', 'pipe:0', '-an', '-filter_complex', filter_graph,
        '-map', '[out]', '-loop', '0', '-f', 'gif', 'pipe:1',
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        result = subprocess.run(
            command, input=bytes(webm_bytes), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, creationflags=creationflags)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("Не удалось запустить конвертацию WebM → GIF: %s", exc)
        return None

    if result.returncode != 0:
        error = result.stderr.decode('utf-8', errors='replace').strip()
        logging.warning("FFmpeg не преобразовал WebM-стикер в GIF: %s", error[:500])
        return None
    if not (result.stdout.startswith(b'GIF87a') or result.stdout.startswith(b'GIF89a')):
        logging.warning("FFmpeg вернул данные, которые не являются GIF.")
        return None
    return result.stdout
