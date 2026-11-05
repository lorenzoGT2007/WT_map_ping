@echo off
rem Double-click to start WT Map Ping without a console window (needs Python from python.org).
where pyw >nul 2>nul && (start "" pyw "%~dp0wt_map_ping.py" & exit /b)
echo WT Map Ping needs Python. Get it from https://www.python.org/downloads/ (see Installation in README.md).
pause
