@echo off
rem Yuketang auto-detect + auto-learn launcher (ASCII only on purpose: cmd codepage safe)
rem
rem   run.bat --help
rem   run.bat --auto --cookies "csrftoken=..; sessionid=.."
rem   run.bat --auto                  (cookie already in config.json)
rem   run.bat --plan --course ???
rem
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="

rem 1) manual override: set YKT_PYTHON=C:\path\python.exe
if defined YKT_PYTHON (
    "%YKT_PYTHON%" -c "import sys" >nul 2>&1 && set "PY=%YKT_PYTHON%"
)

rem 2) DSH bundled runtime
if not defined PY (
    for %%P in (
        "%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
    ) do (
        if not defined PY if exist "%%~P" (
            "%%~P" -c "import sys" >nul 2>&1 && set "PY=%%~P"
        )
    )
)

rem 3) system py launcher / python
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo.
    echo [X] No usable Python 3.9+ found.
    echo     Install: https://www.python.org/downloads/windows/  ^(check "Add python.exe to PATH"^)
    echo     Or: set YKT_PYTHON=C:\Python312\python.exe  then run run.bat again
    echo     Note: the python.exe in Microsoft Store stub does nothing until Python is installed.
    echo.
    exit /b 2
)

echo [i] python: %PY%
%PY% main.py %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :eof
if "%RC%"=="2" echo [X] exit 2: cookie missing - see the hint above
if "%RC%"=="3" echo [X] exit 3: session expired - grab a fresh cookie
if "%RC%"=="4" echo [X] exit 4: endpoint detection failed - try --dump --redetect
if "%RC%"=="5" echo [X] exit 5: no course selected - use --course / --index / --auto
if "%RC%"=="6" echo [!] exit 6: some leaves failed - rerun once usually finishes them
exit /b %RC%
