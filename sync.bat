@echo off
rem Commit everything the .gitignore whitelist allows and push it to GitHub.
rem   sync.bat                  -> commit message "sync"
rem   sync.bat "what changed"   -> your own message
cd /d "%~dp0"
set "MSG=%~1"
if "%MSG%"=="" set "MSG=sync"
git add -A
git diff --cached --quiet && echo Nothing new to commit.
git diff --cached --quiet || git commit -q -m "%MSG%"
git push -q origin main && echo Pushed to github.com/weeef/ai-command-center
if errorlevel 1 echo Push failed - check your connection and run sync.bat again.
if "%~1"=="" pause
