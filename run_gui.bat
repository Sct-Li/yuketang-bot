@echo off
rem Yuketang GUI launcher (ASCII only on purpose: cmd codepage safe)
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
if defined YKT_PYTHON (
    "%YKT_PYTHON%" -c "import sys" >nul 2>&1 && set "PY=%YKT_PYTHON%"
)
if not defined PY (
    for %%P in (
        "%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
    ) do (
        if not defined PY if exist "%%~P" (
            "%%~P" -c "import sys" >nul 2>&1 && set "PY=%%~P"
        )
    )
)
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo [X] No usable Python 3.9+ found.
    echo     Install: https://www.python.org/downloads/windows/
    echo     Or: set YKT_PYTHON=C:\Python312\python.exe
    echo.
    pause
    exit /b 2
)

rem prefer pythonw.exe so no console window stays behind
set "PYW=%PY%"
echo %PY% | findstr /i "python.exe" >nul && (
    for %%D in ("%PY%") do set "PYW=%%~dpDpythonw.exe"
)
if exist "%PYW%" (
    start "" "%PYW%" gui.py
) else (
    %PY% gui.py
)
exit /b 0
