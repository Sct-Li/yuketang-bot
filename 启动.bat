@echo off
rem 源码直跑（不打包）—— 双击即可，无控制台窗口
rem 需要本机有 Python 3.9+；找不到会提示。
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
if defined YKT_PYTHON (
    "%YKT_PYTHON%" -c "import sys" >nul 2>&1 && set "PY=%YKT_PYTHON%"
)
if not defined PY (
    for %%P in (
        "%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Python312\python.exe"
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
    echo [X] 没找到 Python 3.9+。
    echo     装一个：https://www.python.org/downloads/windows/  ^(勾 Add python.exe to PATH^)
    echo     或 set YKT_PYTHON=C:\Python312\python.exe 后重跑本文件
    echo.
    pause
    exit /b 2
)

rem 优先 pythonw，避免留一个黑框；找不到就用 python
set "PYW=%PY%"
echo %PY% | findstr /i "python.exe" >nul && (
    for %%D in ("%PY%") do set "PYW=%%~dpDpythonw.exe"
)
if exist "%PYW%" (
    start "" "%PYW%" gui.py %*
) else (
    %PY% gui.py %*
)
exit /b 0
