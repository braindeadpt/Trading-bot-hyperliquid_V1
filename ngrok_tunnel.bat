@echo off
chcp 65001 >nul
title ngrok dashboard tunnel

REM Public dashboard share — ngrok static dev domain -> localhost:5000.
REM Dashboard auth (BOT_DASHBOARD_TOKEN) is the access barrier; ngrok
REM only transports. Task: Hyperliquid-Dashboard-Tunnel (logon trigger,
REM hidden via scripts\ops\run_hidden.vbs). ngrok itself auto-reconnects
REM on network blips; the loop below restarts it only if the process
REM exits (crash/kill).

cd /d "%~dp0"
:loop
tools\ngrok.exe http --url=remedial-deception-contact.ngrok-free.dev 5000 >> logs\ngrok_tunnel.log 2>&1
ping 127.0.0.1 -n 11 >nul
goto loop
