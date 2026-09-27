@echo off
chcp 65001 > nul
title TeekaGramAi
cd /d "%~dp0"

:: Запуск одного бота. Первый запуск сам поставит зависимости.
:: Аргументы пробрасываются в main.py, например:  start.bat --account 2

if not exist ".env" (
    echo.
    echo   Нет файла .env — создаю шаблон.
    echo.
    > .env echo INSTANCE_NUMBER=1
    >> .env echo TELAGRAMM_API_ID=
    >> .env echo TELAGRAMM_API_HASH=
    echo   Откройте .env и впишите API_ID и API_HASH.
    echo   Взять их тут: https://my.telegram.org  ^-^>  API development tools
    echo.
    notepad .env
    pause
    exit /b 1
)

python --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python не найден. Установите его с python.org
    echo   и обязательно поставьте галочку "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

:: Проверяем сами импорты: метка могла пропасть, а зависимости уже установлены.
python -c "import flask, telethon, dotenv, colorama, PIL, aiohttp, emoji, openai, imageio_ffmpeg; from google import genai" > nul 2>&1
if errorlevel 1 (
    echo.
    echo   Первый запуск: устанавливаю зависимости, это займёт минуту...
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   Не удалось установить зависимости. Смотрите ошибку выше.
        pause
        exit /b 1
    )
    :: Старый SDK конфликтует с новым — убираем, если остался.
    python -m pip uninstall -y google-generativeai > nul 2>&1
)

:: Общий счётчик бесплатных токенов OpenAI — одна библиотека на все проекты.
python -c "import openai_budget" > nul 2>&1
if errorlevel 1 if exist "%USERPROFILE%\openai_budget\pyproject.toml" (
    echo   Ставлю общую библиотеку openai_budget...
    python -m pip install -e "%USERPROFILE%\openai_budget" -q
    if errorlevel 1 echo   Не удалось установить openai_budget. Проверка бесплатного лимита будет недоступна.
)


:: Общий учёт Gemini и список бесплатных моделей — вне папки проекта.
python -c "import gemini_budget; assert callable(gemini_budget.free_models)" > nul 2>&1
if errorlevel 1 (
    if not exist "%USERPROFILE%\gemini_budget\pyproject.toml" (
        echo   Не найдена общая библиотека Gemini: %USERPROFILE%\gemini_budget
        echo   Установите её отдельно перед запуском программы.
        pause
        exit /b 1
    )
    echo   Ставлю общую библиотеку gemini_budget...
    python -m pip install -e "%USERPROFILE%\gemini_budget" -q
    if errorlevel 1 (
        echo   Не удалось установить gemini_budget. Смотрите ошибку выше.
        pause
        exit /b 1
    )
)

python main.py --auto-account %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
if "%APP_EXIT_CODE%"=="0" exit /b 0

echo.
echo   Программа остановлена с ошибкой %APP_EXIT_CODE%. Смотрите сообщение выше.
pause
exit /b %APP_EXIT_CODE%
