@echo off
cd /d "%~dp0"
python -c "import telethon,python_socks,qrcode,PIL,aiohttp" 2>NUL || python -m pip install -r requirements.txt
start "" pythonw app.py
