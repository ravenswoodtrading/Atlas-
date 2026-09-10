@echo off
set ATLAS_AUTOMATION_ENABLED=0
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
pause
