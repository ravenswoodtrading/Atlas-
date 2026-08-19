@echo off
REM Self-healing Cloudflare Quick Tunnel launcher for Atlas -- exposes
REM localhost:8000 to the public internet so the Google Sheet's Apps
REM Script webhook trigger can reach it. Mirrors start_atlas_loop.bat's
REM restart-on-exit pattern.
REM
REM IMPORTANT: this is a QUICK tunnel (no Cloudflare account/domain) --
REM its public URL is randomly assigned and CHANGES every time this
REM restarts. Check cloudflare_tunnel.log for the current
REM "https://....trycloudflare.com" URL and re-paste it into the Apps
REM Script's WEBHOOK_URL constant if it ever changes.

cd /d "%~dp0"

:loop
echo [%date% %time%] Starting Cloudflare tunnel... >> cloudflare_tunnel.log
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --protocol http2 --url http://localhost:8000 >> cloudflare_tunnel.log 2>&1
echo [%date% %time%] Tunnel exited -- restarting in 5s... >> cloudflare_tunnel.log
timeout /t 5 /nobreak >nul
goto loop
