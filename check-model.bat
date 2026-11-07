@echo off
rem Shows which model the main server on port 8090 is running.
powershell -NoProfile -Command "try { $k=(Get-Content -LiteralPath '%~dp0api-key.txt' -First 1).Trim(); $m=(Invoke-RestMethod 'http://127.0.0.1:8090/props' -Headers @{Authorization=('Bearer '+$k)} -TimeoutSec 3).model_path; if ($m -match 'uncensored') { 'Running: UNCENSORED model' } else { 'Running: STANDARD model' }; Split-Path $m -Leaf } catch { 'No model is running on port 8090.' }"
if "%~1"=="" pause
