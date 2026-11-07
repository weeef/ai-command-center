@echo off
rem VS Code window whose Cline uses the laptop through PAIR (port 8080). Settings are separate from your main VS Code.
set "CLINE_DIR=%~dp0cline-laptop"
call code --user-data-dir "%~dp0vscode-laptop" %*
