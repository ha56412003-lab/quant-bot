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
rem ===== 本地 = 测试盘（2026-11-11 身份分离）=====
rem 1) 数据库指向测试库：本地怎么折腾都不污染正式流水（正式库只由服务器写）
rem 2) 日目标关闭：yaml 两边共用，靠环境变量关，避免两边各报各的"今日目标完成"
rem 3) 页面标记：前端顶栏显示橙色「本地测试」，一眼分清在看哪台
set QB_DB_PATH=quantbot_test.db
set QB_DAILY_TARGET_ENABLED=0
set QB_ENV_LABEL=本地测试
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8899 >> logs\server.log 2>&1
