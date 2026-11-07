@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem 币安 fapi 国内直连不通（httpx.PoolTimeout），行情/下单必须走本机代理
set HTTP_PROXY=http://127.0.0.1:7897
set HTTPS_PROXY=http://127.0.0.1:7897
rem 本机回环不要走代理
set NO_PROXY=localhost,127.0.0.1,::1
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8899 >> logs\server.log 2>&1
