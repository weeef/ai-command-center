# First-time setup for the local AI command center (Windows 10/11 + NVIDIA GPU).
# Run setup.bat (it calls this). Safe to run again: it skips everything that's already there.
#
# What it does:
#   1. checks the GPU and Python, installs numpy (optional speed-up for memory search)
#   2. downloads llama.cpp (official CUDA build from github.com/ggml-org/llama.cpp) if it isn't there
#   3. creates api-key.txt (a random key for the local model server)
#   4. downloads the models you pick from Hugging Face, plus the small memory-search model
#   5. writes llm-config.bat with settings sized for YOUR graphics card
#   6. puts a "Command Center" shortcut on your desktop
param([switch]$SkipModels)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Note($t) { Write-Host "   $t" -ForegroundColor Yellow }
function Fetch($url, $dest) {
    if (Test-Path $dest) { Ok "already have $(Split-Path -Leaf $dest)"; return }
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Write-Host "   downloading $(Split-Path -Leaf $dest)"
    & curl.exe -L --fail --retry 5 -C - -o "$dest.part" $url
    if ($LASTEXITCODE -ne 0) { throw "download failed: $url" }
    Move-Item -Force "$dest.part" $dest
}

Write-Host "Local AI command center - setup" -ForegroundColor White
Write-Host "Folder: $Root"

# ------------------------------------------------------------------ 1. GPU + Python
Step "Checking your PC"
$vramGB = 0; $driver = 0
try {
    $q = (& nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits) -split "`n" | Select-Object -First 1
    $p = $q -split ",\s*"
    $vramGB = [math]::Round([double]$p[1] / 1024, 1); $driver = [double]($p[2] -split "\.")[0]
    Ok "GPU: $($p[0]) with $vramGB GB, driver $($p[2])"
} catch {
    Note "No NVIDIA GPU found (nvidia-smi missing). The models will run on the CPU only and be very slow."
}
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Ok "RAM: $ramGB GB"
if ($ramGB -lt 32) { Note "The 35B models need about 24-28 GB of RAM plus VRAM. With $ramGB GB, pick a smaller model (see README)." }

$py = $null
foreach ($c in @("py -3", "python")) {
    try {
        $v = & cmd /c "$c -c ""import sys;print(sys.version_info[0]*100+sys.version_info[1])"" 2>nul"
        if ($v -and [int]$v -ge 310) { $py = $c; break }
    } catch {}
}
if (-not $py) {
    Note "Python 3.10 or newer is required. Install it with:  winget install Python.Python.3.13"
    Note "(or from python.org, tick 'Add python.exe to PATH'), then run setup.bat again."
    exit 1
}
Ok "Python: $(& cmd /c "$py --version")"
& cmd /c "$py -m pip install --user --quiet --disable-pip-version-check numpy anthropic" | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "numpy + anthropic installed (faster memory search; optional Claude web search)" } else { Note "numpy/anthropic didn't install; memory still works, Claude web search won't" }

# ------------------------------------------------------------------ 2. llama.cpp
Step "llama.cpp (the engine that runs the models)"
$llama = Join-Path $Root "llama.cpp\llama-server.exe"
if (Test-Path $llama) {
    Ok "already installed"
} else {
    $rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" -Headers @{ "User-Agent" = "command-center-setup" }
    $flavor = if ($vramGB -eq 0) { "cpu" } elseif ($driver -ge 580) { "cuda-13" } else { "cuda-12" }
    $main = $rel.assets | Where-Object { $_.name -like "llama-*-bin-win-$flavor*-x64.zip" } | Select-Object -First 1
    if (-not $main) { throw "couldn't find a Windows $flavor build in llama.cpp release $($rel.tag_name)" }
    $tmp = Join-Path $env:TEMP "cc-llama"; New-Item -ItemType Directory -Force $tmp | Out-Null
    Fetch $main.browser_download_url (Join-Path $tmp $main.name)
    Expand-Archive -Force (Join-Path $tmp $main.name) (Join-Path $Root "llama.cpp")
    if ($flavor -ne "cpu") {
        $rt = $rel.assets | Where-Object { $_.name -like "cudart-llama-bin-win-$flavor*-x64.zip" } | Select-Object -First 1
        if ($rt) { Fetch $rt.browser_download_url (Join-Path $tmp $rt.name); Expand-Archive -Force (Join-Path $tmp $rt.name) (Join-Path $Root "llama.cpp") }
    }
    # some releases put the files in a subfolder
    $found = Get-ChildItem (Join-Path $Root "llama.cpp") -Recurse -Filter llama-server.exe | Select-Object -First 1
    if ($found -and $found.DirectoryName -ne (Join-Path $Root "llama.cpp")) {
        Get-ChildItem $found.DirectoryName | Move-Item -Destination (Join-Path $Root "llama.cpp") -Force
    }
    Remove-Item -Recurse -Force $tmp
    Ok "installed llama.cpp $($rel.tag_name) ($flavor)"
}

# ------------------------------------------------------------------ 3. API key
Step "API key for the local model server"
$keyFile = Join-Path $Root "api-key.txt"
if (Test-Path $keyFile) { Ok "api-key.txt exists" } else {
    $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($keyFile, ([BitConverter]::ToString($bytes) -replace "-", "").ToLower())
    Ok "created api-key.txt (keep it private; the command center adds it for you)"
}

# ------------------------------------------------------------------ 4. models
$HF = "https://huggingface.co"
$std   = "models\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
$proj  = "models\Qwen3.6-35B-A3B-GGUF\mmproj-F32.gguf"
$unc   = "models\Qwen3.6-35B-A3B-uncensored-heretic-GGUF\Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf"
$emb   = "models\Qwen3-Embedding-0.6B-GGUF\Qwen3-Embedding-0.6B-Q8_0.gguf"
if (-not $SkipModels) {
    Step "Models (downloaded from Hugging Face)"
    Write-Host "   1. standard   Qwen3.6-35B-A3B Q4_K_M  (22 GB)  - recommended"
    Write-Host "   2. uncensored Qwen3.6-35B-A3B heretic Q5_K_M (25 GB)"
    Write-Host "   3. both"
    Write-Host "   4. skip (I'll add my own; see README > Using other models)"
    $free = [math]::Round((Get-PSDrive ((Get-Item $Root).PSDrive.Name)).Free / 1GB)
    Write-Host "   free space here: $free GB"
    $pick = Read-Host "   which? [1-4]"
    if ($pick -in @("1", "3")) {
        Fetch "$HF/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf" (Join-Path $Root $std)
        Fetch "$HF/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F32.gguf" (Join-Path $Root $proj)
    }
    if ($pick -in @("2", "3")) {
        Fetch "$HF/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-GGUF/resolve/main/Qwen3.6-35B-A3B-uncensored-heretic.Q5_K_M.gguf" (Join-Path $Root $unc)
        Fetch "$HF/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F32.gguf" (Join-Path $Root $proj)
    }
    Step "Memory search model (0.6 GB, runs on the CPU)"
    Fetch "$HF/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf" (Join-Path $Root $emb)
}

# ------------------------------------------------------------------ 5. settings for this GPU
Step "Sizing the model settings for your graphics card"
$cfg = Join-Path $Root "llm-config.bat"
if (Test-Path $cfg) {
    Ok "llm-config.bat already exists, leaving it alone (delete it and re-run to recalculate)"
} else {
    # Measured on the reference PC (RTX 5080 16 GB), peak VRAM with -ub 2048 + ngram drafting, minus ~0.85 GB the desktop
    # uses: Q4 at --n-cpu-moe 24 ~12.9 GB, Q5 at 26 ~12.6 GB; each expert layer moved to the GPU costs ~0.46 GB (Q4) /
    # ~0.56 GB (Q5); -ub 512 instead of 2048 saves ~1.5 GB. Keep ~2 GB free for Windows, the browser and other apps.
    # 40 = every expert layer in system RAM.
    $budget = [math]::Max(0, $vramGB - 2.0)
    function MoeFor($base, $atMoe, $perLayer, $extra) {
        for ($m = 0; $m -le 40; $m++) { if ($base + $perLayer * ($atMoe - $m) + $extra -le $budget) { return $m } }
        return 40
    }
    $big = $vramGB -ge 12
    $save = if ($big) { 0 } else { 1.5 }
    $moeStd = MoeFor (12.9 - $save) 24 0.464 0
    $moeUnc = MoeFor (12.6 - $save) 26 0.56 0
    $ub = if ($big) { 2048 } else { 512 }
    $spec = "--spec-type ngram-mod"
    if ($vramGB -eq 0) { $ngl = "0" } else { $ngl = "99" }
    $cores = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum
    $cacheRam = if ($ramGB -ge 64) { 16384 } elseif ($ramGB -ge 48) { 8192 } else { 2048 }
    @"
@echo off
rem Written by setup.ps1 for a $vramGB GB GPU. start-llm.bat reads this. Edit to tune:
rem   MOE_*  : expert layers kept in system RAM (lower = faster but more VRAM; 40 = all in RAM)
rem   UB     : prompt batch size (2048 reads long prompts ~2x faster than 512, costs ~1.7 GB VRAM)
rem   SPEC   : speculative decoding; ngram-mod makes Cline-style code rewrites ~2.4x faster
set "MOE_STD=$moeStd"
set "MOE_UNC=$moeUnc"
set "MOE_BIG=$([math]::Min(40, $moeStd + 3))"
set "UB=$ub"
set "SPEC=$spec"
set "NGL=$ngl"
set "THREADS=$cores"
set "CACHE_RAM=$cacheRam"
"@ | Set-Content -Encoding ascii $cfg
    Ok "llm-config.bat: standard keeps $moeStd expert layers in RAM, uncensored $moeUnc, batch $ub $(if ($spec) { '+ ngram drafting' })"
}

# ------------------------------------------------------------------ 6. shortcut
Step "Desktop shortcut"
$pyw = (& cmd /c "$py -c ""import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))""").Trim()
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Command Center.lnk"
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sh.TargetPath = $pyw
$sh.Arguments = '"' + (Join-Path $Root "command-center\launch.pyw") + '"'
$sh.WorkingDirectory = Join-Path $Root "command-center"
$sh.IconLocation = Join-Path $Root "command-center\command-center.ico"
$sh.Save()
Ok "created 'Command Center' on your desktop (right-click it > Pin to taskbar if you like)"

Step "Done"
Write-Host "   1. Double-click 'Command Center' on the desktop."
Write-Host "   2. In 1:llm press 'on' to start the model (first start takes ~30 s), then chat."
Write-Host "   3. Optional extras (phone access, VS Code + Cline, memory on another drive): see README.md"
