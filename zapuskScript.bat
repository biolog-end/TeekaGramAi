@echo off
chcp 65001 > nul
setlocal

:: =================================================================
:: TeekaGramAi Multi-Bot Launcher by Ttekas
:: =================================================================
:: Этот скрипт запускает несколько копий программы из разных папок
:: Нужно этот скрипт разместить выше всех папок на 1 директорию
:: Вся настройка (ключи, порты) должна находиться в файлах .env внутри каждой папки
:: =================================================================

:: --- ОБЩИЕ НАСТРОЙКИ ---

:: Определяем путь к папке, где лежит этот .bat файл. Все пути будут строиться отсюда
set "BASE_PATH=%~dp0"

:: (Опционально) Впишите ID чата, чтобы он открылся в браузере для всех ботов
:: Оставьте ПУСТЫМ, чтобы открывались главные страницы выбора чатов
:: Пример: set "TARGET_CHAT_ID=-1002398372400"
set "TARGET_CHAT_ID=-1002398372400"
:: Уже выставлен чат слей"


:: --- НАСТРОЙКА ЭКЗЕМПЛЯРОВ БОТОВ ---
:: Укажите здесь папки с вашими ботами и номер аккаунта для авто-выбора

:: --- БОТ 1 ---
set "BOT1_FOLDER=ai_telegramm_1"
set "BOT1_ACCOUNT_CHOICE=1"
set "BOT1_CONSOLE_TITLE=Bot 1 - mahir"
set "BOT1_COLOR=0A"

:: --- БОТ 2 ---
set "BOT2_FOLDER=ai_telegramm_2"
set "BOT2_ACCOUNT_CHOICE=1"
set "BOT2_CONSOLE_TITLE=Bot 2 - appolon"
set "BOT2_COLOR=09"

:: --- БОТ 3 ---
:: Если бот не нужен, просто закомментируйте его блок с помощью ::
:: set "BOT3_FOLDER=ai_telegramm_3"
:: set "BOT3_ACCOUNT_CHOICE=1"
:: set "BOT3_CONSOLE_TITLE=Bot 3 - Natasha"
:: set "BOT3_COLOR=0B"


:: =================================================================
:: ЛОГИКА ЗАПУСКА (Обычно здесь ничего менять не нужно)
:: =================================================================
echo.
echo  ======================================
echo      TeekaGramAi Multi-Bot Launcher
echo  ======================================
echo.

:: --- Запуск БОТА 1 ---
if defined BOT1_FOLDER (
    if exist "%BASE_PATH%%BOT1_FOLDER%\main.py" (
        echo  Запускаю Бота 1 (%BOT1_CONSOLE_TITLE%)...
        start "%BOT1_CONSOLE_TITLE%" /D "%BASE_PATH%%BOT1_FOLDER%" cmd /K "COLOR %BOT1_COLOR% && (echo %BOT1_ACCOUNT_CHOICE%) | python main.py"
        timeout /t 2 > nul
    ) else (
        echo  [ОШИБКА] Папка для Бота 1 "%BASE_PATH%%BOT1_FOLDER%" или main.py не найден!
    )
)

:: --- Запуск БОТА 2 ---
if defined BOT2_FOLDER (
    if exist "%BASE_PATH%%BOT2_FOLDER%\main.py" (
        echo  Запускаю Бота 2 (%BOT2_CONSOLE_TITLE%)...
        start "%BOT2_CONSOLE_TITLE%" /D "%BASE_PATH%%BOT2_FOLDER%" cmd /K "COLOR %BOT2_COLOR% && (echo %BOT2_ACCOUNT_CHOICE%) | python main.py"
        timeout /t 2 > nul
    ) else (
        echo  [ОШИБКА] Папка для Бота 2 "%BASE_PATH%%BOT2_FOLDER%" или main.py не найден!
    )
)

:: --- Запуск БОТА 3 ---
if defined BOT3_FOLDER (
    if exist "%BASE_PATH%%BOT3_FOLDER%\main.py" (
        echo  Запускаю Бота 3 (%BOT3_CONSOLE_TITLE%)...
        start "%BOT3_CONSOLE_TITLE%" /D "%BASE_PATH%%BOT3_FOLDER%" cmd /K "COLOR %BOT3_COLOR% && (echo %BOT3_ACCOUNT_CHOICE%) | python main.py"
        timeout /t 2 > nul
    ) else (
        echo  [ОШИБКА] Папка для Бота 3 "%BASE_PATH%%BOT3_FOLDER%" или main.py не найден!
    )
)

:: Для большего количества ботов просто код выше скопируй сменив числа с 3 на твоё число


echo.
echo  Все боты запущены. Ожидание 10 секунд для инициализации серверов...
timeout /t 10 > nul

echo.
echo  Открываю веб-интерфейсы в браузере...

:: --- Открытие браузеров ---
if defined TARGET_CHAT_ID ( set "CHAT_URL_SUFFIX=/chat/%TARGET_CHAT_ID%" ) else ( set "CHAT_URL_SUFFIX=/" )

if defined BOT1_FOLDER if exist "%BASE_PATH%%BOT1_FOLDER%\main.py" ( start "Bot 1 Web" http://127.0.0.1:5001%CHAT_URL_SUFFIX% )
if defined BOT2_FOLDER if exist "%BASE_PATH%%BOT2_FOLDER%\main.py" ( start "Bot 2 Web" http://127.0.0.1:5002%CHAT_URL_SUFFIX% )
if defined BOT3_FOLDER if exist "%BASE_PATH%%BOT3_FOLDER%\main.py" ( start "Bot 3 Web" http://127.0.0.1:5003%CHAT_URL_SUFFIX% )

echo.
echo  Готово!
echo.
endlocal
pause