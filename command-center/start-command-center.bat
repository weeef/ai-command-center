@echo off
setlocal
rem Opens the command center UI. Starts its little server hidden if it isn't already answering.
rem Running this twice never starts a second server (the server also refuses to share its port).
set "DIR=%~dp0"
set "URL=http://127.0.0.1:8099/"

set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"

curl.exe -s -o nul -m 2 "%URL%api/ping" && goto open
start "" "%PYW%" "%DIR%server.py"
for /l %%i in (1,1,20) do (
  curl.exe -s -o nul -m 1 "%URL%api/ping" && goto open
  ping -n 2 127.0.0.1 >nul
)
echo The command center server did not start. Try: python "%DIR%server.py"
pause
exit /b 1

:open
start "" "%EDGE%" --app=%URL% --user-data-dir="%DIR%edge-profile" --disable-gpu --no-first-run --disable-sync --disable-background-timer-throttling --window-size=1400,920
