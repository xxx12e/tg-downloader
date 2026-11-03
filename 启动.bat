@echo off
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

rem First run: create a private Python environment in .venv (your global Python is not touched).
rem "python" first = the one you added to PATH; "py" (newest installed) only as a fallback.
if not exist "%PY%" (
  echo Preparing the first run, please wait...
  python -m venv .venv 2>/dev/null || py -3 -m venv .venv 2>NUL
  if not exist "%PY%" goto nopython
)

rem Install dependencies when requirements.lock changed. Versions and hashes are pinned,
rem so a tampered package fails to install instead of running.
fc /b requirements.lock .venv\installed.lock >/dev/null 2>&1
if errorlevel 1 (
  "%PY%" -m pip install --disable-pip-version-check --require-hashes -r requirements.lock || goto pipfail
  rem cryptg only speeds up downloads. Prebuilt packages only: never compile it here
  "%PY%" -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements-speedup.lock || echo cryptg has no prebuilt package for this Python yet, so downloads will be slower. Python 3.11 to 3.14 is recommended.
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
