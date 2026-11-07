@echo off
rem Uncensored model: red window. Refuses to start if a model is already running.
netstat -an | findstr /c:"127.0.0.1:8090" | findstr LISTENING >nul && goto busy
title UNCENSORED model - Qwen3.6-35B Q5 - port 8090
color 4F
call "%~dp0start-llm.bat" 2
exit /b

:busy
echo A model is already running. Close its server window first.
call "%~dp0check-model.bat" nopause
pause
