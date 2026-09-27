@echo off
chcp 65001 > nul
title TeekaGramAi
cd /d "%~dp0"

:: Простой запуск. Всю логику проверок делает start.bat — здесь только красивый ярлык.
:: На первом запуске сам себе создаёт ярлык на рабочем столе с иконкой TeekaGramAi.ico.

if not exist ".shortcut_ok" (
    if exist "%~dp0TeekaGramAi.ico" (
        powershell -NoProfile -Command ^
            "$w=New-Object -ComObject WScript.Shell;" ^
            "$s=$w.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\TeekaGramAi.lnk');" ^
            "$s.TargetPath='%~dp0TeekaGramAi.bat';" ^
            "$s.WorkingDirectory='%~dp0';" ^
            "$s.IconLocation='%~dp0TeekaGramAi.ico';" ^
            "$s.Save()" > nul 2>&1
        if not errorlevel 1 echo ok > .shortcut_ok
    )
)

call "%~dp0start.bat" %*
