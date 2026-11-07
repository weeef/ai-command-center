@echo off
rem Standard model: blue window. Refuses to start if a model is already running.
netstat -an | findstr /c:"127.0.0.1:8090" | findstr LISTENING >nul && goto busy
title STANDARD model - Qwen3.6-35B Q4 - port 8090
color 1F
call "%~dp0start-llm.bat" 1
exit /b

:busy
echo A model is already running. Close its server window first.
call "%~dp0check-model.bat" nopause
pause
