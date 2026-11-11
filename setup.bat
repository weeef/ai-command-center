@echo off
rem First-time setup: checks your PC, downloads llama.cpp + models, sizes settings for your GPU, makes a desktop shortcut.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
