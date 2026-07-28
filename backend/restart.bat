@echo off
taskkill /IM python.exe /F >nul 2>&1
python -m uvicorn app.main:app
