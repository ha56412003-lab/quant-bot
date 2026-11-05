@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8899 >> logs\server.log 2>&1
