@echo off
setlocal
rem Local model server on port 8090 with 128K context. Only one model fits in VRAM at a time.
rem Pick from the menu, or skip it with: start-llm.bat 1   or   start-llm.bat 2

rem Everything it needs is in this folder, including the CUDA runtime DLLs next to llama-server.exe.
set "LLAMA=%~dp0llama.cpp\llama-server.exe"
set "MMPROJ=%~dp0models\Qwen3.6-35B-A3B-GGUF\mmproj-F32.gguf"
set "KEYFILE=%~dp0api-key.txt"

set "PICK=%~1"
if not "%PICK%"=="" goto picked
echo.
echo   1. Qwen3.6-35B standard Q4_K_M    (faster)
echo   2. Qwen3.6-35B uncensored Q5_K_M  (abliterated, slower)
echo   3. Qwen3.6-35B standard Q4_K_M, 256K context  (for huge jobs, slower)
echo.
choice /c 123 /n /m "Which model? [1/2/3] "
set "PICK=%errorlevel%"
:picked
set "CTX=131072"
if "%PICK%"=="1" goto q4
if "%PICK%"=="2" goto q5
if "%PICK%"=="3" goto big
echo Unknown choice: %PICK%
pause
exit /b 1

rem --n-cpu-moe : raise by 1 to free VRAM, lower by 1 to use more (about 464 MiB per layer on Q4, 560 MiB on Q5)
:q4
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
set "MOE=24"
goto run

:q5
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-uncensored-heretic-GGUF\Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf"
set "MOE=26"
goto run

rem 262144 is the model's trained maximum. The bigger KV cache costs about 1360 MiB of VRAM, so 3 more layers stay in RAM.
:big
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
set "MOE=27"
set "CTX=262144"
goto run

:run
if not exist "%LLAMA%" ( echo llama-server not found: %LLAMA% & pause & exit /b 1 )
if not exist "%MODEL%" ( echo Model not found: %MODEL% & pause & exit /b 1 )
if not exist "%MMPROJ%" ( echo Vision model not found: %MMPROJ% & pause & exit /b 1 )
if not exist "%KEYFILE%" ( echo API key file not found: %KEYFILE% & pause & exit /b 1 )

rem Port 8080 belongs to NVIDIA PAIR. --load-mode none copies CPU weights into RAM; much faster prompts than mmap.
rem --no-mmproj-offload : vision runs on the CPU; a few seconds slower per image, but leaves VRAM for text speed
rem --metrics : token counters for the command center's "tokens burned" readout (read-only, local only)
"%LLAMA%" -m "%MODEL%" ^
  --mmproj "%MMPROJ%" --no-mmproj-offload ^
  --host 127.0.0.1 --port 8090 --api-key-file "%KEYFILE%" ^
  --fit off -ngl 99 --n-cpu-moe %MOE% ^
  -c %CTX% -np 1 --cache-ram 16384 ^
  -fa on -ctk q8_0 -ctv q8_0 ^
  --load-mode none -t 16 -b 2048 -ub 512 ^
  --metrics

if errorlevel 1 pause
