@echo off
cd /d %~dp0
if not exist .env copy .env.example .env
python -c "import PIL" >nul 2>&1 || python -m pip install -r requirements.txt
python server.py
pause
