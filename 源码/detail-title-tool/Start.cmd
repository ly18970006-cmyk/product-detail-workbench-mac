@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    if defined DETAIL_PYTHON (
        call "%DETAIL_PYTHON%" -m venv .venv
    ) else (
        where py.exe >nul 2>&1
        if errorlevel 1 (
            python -m venv .venv
        ) else (
            py -3 -m venv .venv
        )
    )
)
if not exist ".venv\Scripts\python.exe" goto :failed
".venv\Scripts\python.exe" -B -c "import flask,waitress,PIL" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -B app.py --open-browser
if errorlevel 1 goto :failed
exit /b 0
:failed
echo Startup failed. See README.md and the error above.
pause
exit /b 1
