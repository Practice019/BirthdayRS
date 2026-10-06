@echo off
rem ===========================================================
rem  BirthdayRS - double-click launcher for the desktop app
rem
rem  This console is only a launcher. The app opens its own
rem  native window and listens on NO port. Closing the app
rem  window closes this console too.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads .bat using
rem  the system ANSI codepage (GBK on zh-CN Windows), so UTF-8
rem  Chinese comments get split into garbage commands.
rem ===========================================================

chcp 65001 >nul
setlocal

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "EXITCODE=0"

rem ---- prefer uv: the project ships uv.lock for reproducible deps ----
where uv >nul 2>nul
if %errorlevel%==0 (
    uv run python -m src.main app --config config.yml
    set "EXITCODE=%errorlevel%"
    goto finish
)

rem ---- fall back to a project venv ----
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m src.main app --config config.yml
    set "EXITCODE=%errorlevel%"
    goto finish
)

rem ---- finally any python on PATH ----
where python >nul 2>nul
if %errorlevel%==0 (
    python -m src.main app --config config.yml
    set "EXITCODE=%errorlevel%"
    goto finish
)

echo.
echo   Python was not found.
echo.
echo   Choose one of these:
echo     1. Install uv (recommended): https://docs.astral.sh/uv/
echo        Then run in this folder:  uv sync
echo     2. Install Python 3.10+ and run:  pip install -r requirements.txt
echo.
pause
exit /b 1

:finish
if not "%EXITCODE%"=="0" (
    echo.
    echo   The app exited with code %EXITCODE%
    echo   Log: %USERPROFILE%\.birthdayrs\app.log
    echo.
    pause
)

endlocal
exit /b %EXITCODE%
