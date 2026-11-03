@echo off
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

rem First run: create a private Python environment in .venv (your global Python is not touched)
if not exist "%PY%" (
  echo Preparing the first run, please wait...
  py -3 -m venv .venv 2>NUL || python -m venv .venv
  if not exist "%PY%" goto nopython
)

rem Install dependencies when requirements.lock changed. Versions and hashes are pinned,
rem so a tampered package fails to install instead of running.
fc /b requirements.lock .venv\installed.lock >NUL 2>&1
if errorlevel 1 (
  "%PY%" -m pip install --disable-pip-version-check --require-hashes -r requirements.lock || goto pipfail
  copy /y requirements.lock .venv\installed.lock >NUL
)

start "" ".venv\Scripts\pythonw.exe" app.py %*
exit /b 0

:nopython
echo.
echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during setup, then run this file again.
pause
exit /b 1

:pipfail
echo.
echo Installing dependencies failed. Python 3.10+ and a working network/proxy are needed.
echo Fix that and run this file again.
pause
exit /b 1
