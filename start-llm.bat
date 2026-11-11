@echo off
setlocal
rem Local model server on port 8090 with 128K context. Only one model fits in VRAM at a time.
rem Pick from the menu, or skip it with: start-llm.bat 1   (2, 3 ...)
rem
rem Tuning lives in llm-config.bat (written by setup.bat for your GPU, never committed). The defaults below are the
rem measured best for a 16 GB card (RTX 5080) + 64 GB RAM; see README "Speed and settings for your GPU".

set "LLAMA=%~dp0llama.cpp\llama-server.exe"
set "MMPROJ=%~dp0models\Qwen3.6-35B-A3B-GGUF\mmproj-F32.gguf"
set "KEYFILE=%~dp0api-key.txt"

rem ---- defaults (16 GB reference card); llm-config.bat overrides any of them
set "MOE_STD=23"
set "MOE_UNC=25"
set "MOE_BIG=26"
set "UB=2048"
set "SPEC=--spec-type ngram-mod"
set "NGL=99"
set "THREADS=16"
set "CACHE_RAM=16384"
if exist "%~dp0llm-config.bat" call "%~dp0llm-config.bat"

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

rem MOE_* = --n-cpu-moe: raise by 1 to free VRAM, lower by 1 to use more (about 464 MiB per layer on Q4, 560 MiB on Q5)
:q4
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
set "MOE=%MOE_STD%"
goto run

:q5
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-uncensored-heretic-GGUF\Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf"
set "MOE=%MOE_UNC%"
goto run

rem 262144 is the model's trained maximum. The bigger KV cache costs about 1360 MiB of VRAM, so 3 more layers stay in RAM.
:big
set "MODEL=%~dp0models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
set "MOE=%MOE_BIG%"
set "CTX=262144"
goto run

:run
if not exist "%LLAMA%" ( echo llama-server not found: %LLAMA%   - run setup.bat & pause & exit /b 1 )
if not exist "%MODEL%" ( echo Model not found: %MODEL%   - run setup.bat to download it & pause & exit /b 1 )
if not exist "%KEYFILE%" ( echo API key file not found: %KEYFILE%   - run setup.bat & pause & exit /b 1 )
set "VISION="
if exist "%MMPROJ%" ( set VISION=--mmproj "%MMPROJ%" --no-mmproj-offload ) else ( echo Vision model not found, starting without image support. )

rem Port 8080 belongs to NVIDIA PAIR. --load-mode none copies CPU weights into RAM; much faster prompts than mmap.
rem --no-mmproj-offload : vision runs on the CPU; a few seconds slower per image, but leaves VRAM for text speed
rem -ub 2048 : reads long prompts (Cline, long chats) ~2.3x faster than 512 (measured), for ~1.5 GB VRAM
rem --spec-type ngram-mod : drafts text that repeats earlier context; Cline-style rewrites ~2.4x faster (measured)
rem --metrics : token counters for the command center's "tokens burned" readout (read-only, local only)
"%LLAMA%" -m "%MODEL%" %VISION% ^
  --host 127.0.0.1 --port 8090 --api-key-file "%KEYFILE%" ^
  --fit off -ngl %NGL% --n-cpu-moe %MOE% ^
  -c %CTX% -np 1 --cache-ram %CACHE_RAM% ^
  -fa on -ctk q8_0 -ctv q8_0 ^
  --load-mode none -t %THREADS% -b 2048 -ub %UB% %SPEC% ^
  --metrics

if errorlevel 1 pause
