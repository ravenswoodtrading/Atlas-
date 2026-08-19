@echo off
REM Self-healing Atlas launcher. Runs uvicorn in a loop -- if it ever
REM exits for any reason (crash, killed, port conflict cleared, etc.),
REM it's restarted automatically after a short pause instead of
REM silently staying down. Bound to 0.0.0.0 so it's reachable from
REM other devices on the network (see run.bat for localhost-only).
REM Intended to be launched once, detached, by the "Atlas Server"
REM Scheduled Task (runs at logon) -- see main conversation for setup.

cd /d "%~dp0"

:loop
echo [%date% %time%] Starting Atlas... >> atlas_server.log
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> atlas_server.log 2>&1
echo [%date% %time%] Atlas exited -- restarting in 5s... >> atlas_server.log
timeout /t 5 /nobreak >nul
goto loop
