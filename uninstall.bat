@echo off
rem Uninstall: lists everything the command center installed (with sizes) and removes what you pick. Asks before deleting.
rem Options: -All, -Everything (prerequisites too), -ModelsOnly, -KeepMemory, -KeepModels, -Only a,b, -NoExport, -Yes, -DryRun
rem "& exit /b" on the same line: cmd must not read this file again after the uninstaller has deleted it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %* & exit /b
