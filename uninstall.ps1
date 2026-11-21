# Uninstaller for the local AI command center. Run uninstall.bat (it calls this), or 6:set -> uninstall.
# Lists everything with sizes first and deletes nothing until you confirm.
#
#   uninstall.bat                  interactive: pick a preset or go item by item
#   uninstall.bat -All             everything the command center made (keeps Python, Tailscale, VS Code, Git)
#   uninstall.bat -Everything      the above plus the prerequisites (pip packages, Python, Tailscale, VS Code + Cline, Git)
#   uninstall.bat -ModelsOnly      just free the disk: models + llama.cpp builds
#   -KeepMemory / -KeepModels      leave memories + saved chats / the models folder in place
#   -NoExport                      don't zip memories + chats to the desktop before deleting them
#   -Yes                           don't ask (use with -All / -Everything / -ModelsOnly / -Only)
#   -Only models,engine,...        just these items: app engine models memory shortcuts cline serve worktrees
#                                  pip python tailscale vscode git
#   -DryRun                        only show what would be removed
param([string[]]$Only, [switch]$All, [switch]$Everything, [switch]$ModelsOnly, [switch]$KeepMemory, [switch]$KeepModels,
      [switch]$NoExport, [switch]$Yes, [switch]$DryRun)
$ErrorActionPreference = "Continue"
$Root = (Split-Path -Parent $MyInvocation.MyCommand.Path).TrimEnd("\")
$CC = Join-Path $Root "command-center"
Set-Location $env:TEMP   # never hold the folder we're deleting

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Note($t) { Write-Host "   $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "   $t" -ForegroundColor Red }
function Has($s, $sub) { return ($s -and $sub -and $s.IndexOf($sub, [StringComparison]::OrdinalIgnoreCase) -ge 0) }
function SizeOf($paths) {
    $n = 0
    foreach ($p in $paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $i = Get-Item -LiteralPath $p -Force
        if ($i.PSIsContainer) { $n += (Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }
        else { $n += $i.Length }
    }
    return [double]$n
}
function Fmt($b) { if ($b -ge 1GB) { "{0:N1} GB" -f ($b / 1GB) } elseif ($b -ge 1MB) { "{0:N0} MB" -f ($b / 1MB) } elseif ($b -gt 0) { "{0:N0} KB" -f ($b / 1KB) } else { "" } }
function Ask($q, $def) {
    if ($Yes) { return $def }
    $a = Read-Host "   $q $(if ($def) { '[Y/n]' } else { '[y/N]' })"
    if (-not $a) { return $def }
    return $a -match "^(y|yes)$"
}
function RemovePath($p) {
    if (-not (Test-Path -LiteralPath $p)) { return $true }
    if ($DryRun) { Write-Host "   would delete $p"; return $true }
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $p) {
        $i = Get-Item -LiteralPath $p -Force
        if ($i.PSIsContainer) { & cmd /c "rmdir /s /q `"$p`"" 2>$null } else { & cmd /c "del /f /q `"$p`"" 2>$null }
    }
    if (Test-Path -LiteralPath $p) { Bad "couldn't delete $p (in use?)"; return $false }
    return $true
}
function FindPython {
    foreach ($c in @("py -3", "python")) {
        try {
            $v = & cmd /c "$c -c ""import sys;print(sys.version_info[0]*100+sys.version_info[1])"" 2>nul"
            if ($v -and [int]$v -ge 310) { return $c }
        } catch {}
    }
    return $null
}
$Winget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
function WingetIds($pattern, $nameQuery) {
    # ids from `winget list` that match a regex, e.g. Python.Python.3.13
    if (-not $Winget) { return @() }
    $out = & winget list --name $nameQuery --accept-source-agreements --disable-interactivity 2>$null | Out-String
    return @([regex]::Matches($out, $pattern) | ForEach-Object { $_.Value } | Select-Object -Unique)
}
function WingetHas($id) {
    if (-not $Winget) { return $false }
    $out = & winget list --id $id -e --accept-source-agreements --disable-interactivity 2>$null | Out-String
    return (Has $out $id)
}
function WingetRemove($id) {
    if ($DryRun) { Write-Host "   would run: winget uninstall --id $id"; return }
    Write-Host "   winget uninstall $id"
    & winget uninstall --id $id -e --silent --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -eq 0) { Ok "removed $id" } else { Note "winget couldn't remove $id (exit $LASTEXITCODE): remove it in Settings > Apps" }
}

Write-Host "Local AI command center - uninstall" -ForegroundColor White
Write-Host "Folder: $Root"
if (-not (Test-Path (Join-Path $CC "server.py")) -and -not (Test-Path (Join-Path $Root "models"))) {
    Note "This doesn't look like a command center folder (no command-center\server.py or models\). Nothing to do."
    if (-not $Yes) { Read-Host "   press Enter to close" | Out-Null }
    exit 1
}

# ------------------------------------------------------------------ what's here
Step "Looking at what's installed (sizes take a moment for big folders)"
$settings = $null
try { $settings = Get-Content -Raw (Join-Path $CC "settings.json") | ConvertFrom-Json } catch {}

# memory home: settings.json memory_dir, else <root>\memory. Only its own files are deleted, never the whole folder
# (it may be a dedicated drive).
$memHome = if ($settings -and $settings.memory_dir) { $settings.memory_dir } else { Join-Path $Root "memory" }
$memPaths = @("memory.db", "memory.db-wal", "memory.db-shm", "memory.db-journal", "chats", "README.txt") | ForEach-Object { Join-Path $memHome $_ }
$memPaths += Join-Path $CC "chats"   # chats from before the memory folder existed

$engines = @(Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "llama.cpp*" } | ForEach-Object { $_.FullName })

# the program, its settings, keys, logs, browser profile and the laptop VS Code / Cline profile
$appNames = @(".git", ".gitignore", "README.md", "setup.bat", "setup.ps1", "sync.bat", "check-model.bat", "start-llm.bat",
    "start-llm.bat.bak", "start-standard.bat", "start-uncensored.bat", "start-command-center.bat", "start-vscode-laptop.bat",
    "llm-config.bat", "api-key.txt", "command-center", "docs", "bench", "benchmarks", "tests", "cline-laptop", "vscode-laptop")
$appPaths = @($appNames | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path -LiteralPath $_ })
$selfPaths = @((Join-Path $Root "uninstall.ps1"), (Join-Path $Root "uninstall.bat"))

# shortcuts that run launch.pyw from this folder: desktop, start menu, taskbar pins
$shell = New-Object -ComObject WScript.Shell
$lnkDirs = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"),
    [Environment]::GetFolderPath("Programs"), (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
    (Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch"))
$shortcuts = @()
foreach ($d in $lnkDirs) {
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -Force -ErrorAction SilentlyContinue) {
        try { $s = $shell.CreateShortcut($f.FullName) } catch { continue }
        if ((Has $s.Arguments $CC) -or (Has $s.TargetPath $Root) -or (Has $s.WorkingDirectory $CC)) { $shortcuts += $f.FullName }
    }
}
$shortcuts = @($shortcuts | Select-Object -Unique)

# main Cline: routed through the command center (5:cline)? the original settings were saved next to it
$clineDir = Join-Path $env:USERPROFILE ".cline\data"
$clineGs = Join-Path $clineDir "globalState.json"
$clineBak = "$clineGs.bak-command-center"
$clineNow = ""
try { $clineNow = (Get-Content -Raw $clineGs | ConvertFrom-Json).openAiBaseUrl } catch {}
$clineOurs = (Has $clineNow "127.0.0.1:8099") -or (Has $clineNow "127.0.0.1:8090")

# phone access: tailscale serve pointing at the command center / model server
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts -and (Test-Path "C:\Program Files\Tailscale\tailscale.exe")) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
$serveOurs = $false
if ($ts) { $sv = & $ts serve status --json 2>$null | Out-String; $serveOurs = (Has $sv "127.0.0.1:8099") -or (Has $sv "127.0.0.1:8090") }

# "open both" worktrees: <project>-laptop on branch cline-laptop
$worktrees = @()
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if ($git -and $settings -and $settings.recent_projects) {
    foreach ($p in @($settings.recent_projects)) {
        $pp = if ($p -is [string]) { $p } else { $p.path }
        if (-not $pp) { continue }
        $wt = $pp.TrimEnd("\", "/") + "-laptop"
        if (Test-Path -LiteralPath (Join-Path $wt ".git")) { $worktrees += [pscustomobject]@{ repo = $pp; wt = $wt } }
    }
}

$py = FindPython
$pyPkgs = @()
if ($py) { foreach ($m in "numpy", "anthropic") { & cmd /c "$py -m pip show $m >nul 2>nul"; if ($LASTEXITCODE -eq 0) { $pyPkgs += $m } } }
Write-Host "   checking installed programs with winget..."
$pyIds = @(WingetIds "Python\.Python\.3\.\d+|Python\.Launcher" "Python")
$hasTs = WingetHas "Tailscale.Tailscale"
$hasCode = WingetHas "Microsoft.VisualStudioCode"
$hasGit = WingetHas "Git.Git"

$items = [ordered]@{}
function Add($key, $label, $size, $present, $group, $detail) {
    $items[$key] = [pscustomobject]@{ key = $key; label = $label; size = $size; present = [bool]$present; group = $group; detail = $detail; on = $false }
}
Add "app"       "Command center program, settings, API key, logs, browser + laptop VS Code profiles" (SizeOf $appPaths) ($appPaths.Count -gt 0) "app" $Root
Add "engine"    "llama.cpp engine builds ($($engines.Count))" (SizeOf $engines) ($engines.Count -gt 0) "app" (($engines | ForEach-Object { Split-Path -Leaf $_ }) -join ", ")
Add "models"    "Models" (SizeOf @(Join-Path $Root "models")) (Test-Path (Join-Path $Root "models")) "app" (Join-Path $Root "models")
Add "memory"    "Memories + saved chats" (SizeOf $memPaths) (@($memPaths | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) "app" $memHome
Add "shortcuts" "Shortcuts (desktop / start menu / taskbar): $($shortcuts.Count)" 0 ($shortcuts.Count -gt 0) "app" (($shortcuts | ForEach-Object { Split-Path -Leaf (Split-Path $_) }) -join ", ")
Add "cline"     "Point your main Cline back at its original provider (it's set to $clineNow)" 0 ($clineOurs) "app" $clineGs
Add "serve"     "Turn off phone access (tailscale serve -> the command center)" 0 $serveOurs "app" ""
Add "worktrees" "Laptop git worktrees from 'open both' ($($worktrees.Count)); only clean ones are removed" 0 ($worktrees.Count -gt 0) "extra" (($worktrees | ForEach-Object { $_.wt }) -join ", ")
Add "pip"       "Python packages setup installed: $($pyPkgs -join ', ')" 0 ($pyPkgs.Count -gt 0) "prereq" "pip --user"
Add "python"    "Python ($($pyIds -join ', '))" 0 ($pyIds.Count -gt 0) "prereq" "other programs may use it"
Add "tailscale" "Tailscale (signs this PC out of your tailnet)" 0 $hasTs "prereq" ""
Add "vscode"    "VS Code + the Cline extension + Cline's data (~\.cline: task history, its settings)" 0 ($hasCode -or (Test-Path (Join-Path $env:USERPROFILE ".cline"))) "prereq" ""
Add "git"       "Git" 0 $hasGit "prereq" "other programs may use it"

$present = @($items.Values | Where-Object { $_.present })
function Show {
    $n = 0
    foreach ($g in @(@("app", "made by the command center"), @("extra", "in your own projects"), @("prereq", "prerequisites (other programs may use these)"))) {
        $rows = @($present | Where-Object { $_.group -eq $g[0] })
        if (-not $rows.Count) { continue }
        Write-Host "`n   $($g[1]):" -ForegroundColor White
        foreach ($it in $rows) {
            $n++
            $mark = if ($it.on) { "[x]" } else { "[ ]" }
            $sz = Fmt $it.size
            Write-Host ("   {0} {1,2}. {2}{3}" -f $mark, $n, $it.label, $(if ($sz) { "  ($sz)" } else { "" })) -ForegroundColor $(if ($it.on) { "Green" } else { "Gray" })
            if ($it.detail) { Write-Host "            $($it.detail)" -ForegroundColor DarkGray }
        }
    }
}
function Preset($p) {
    foreach ($it in $present) {
        $it.on = switch ($p) {
            "all"        { $it.group -eq "app" }
            "everything" { $it.group -ne "extra" }
            "models"     { $it.key -in @("models", "engine") }
            default      { $false }
        }
    }
    if ($KeepMemory -and $items.Contains("memory")) { $items["memory"].on = $false }
    if ($KeepModels -and $items.Contains("models")) { $items["models"].on = $false }
}

if ($Only) {
    Preset "none"; $Only = @($Only | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
    foreach ($k in $Only) { if ($items.Contains($k)) { $items[$k].on = $items[$k].present } else { Bad "unknown item: $k"; exit 1 } }
}
elseif ($Everything) { Preset "everything" } elseif ($All) { Preset "all" } elseif ($ModelsOnly) { Preset "models" }
elseif ($Yes) { Bad "-Yes needs -All, -Everything, -ModelsOnly or -Only"; exit 1 }
else {
    Show
    Write-Host "`n   What should go?" -ForegroundColor White
    Write-Host "   1. everything the command center made (keeps Python, Tailscale, VS Code, Git)"
    Write-Host "   2. all of it, prerequisites too"
    Write-Host "   3. just free disk space: models + llama.cpp"
    Write-Host "   4. choose item by item"
    Write-Host "   q. quit, change nothing"
    $c = Read-Host "   which? [1-4/q]"
    switch ($c) { "1" { Preset "all" } "2" { Preset "everything" } "3" { Preset "models" } "4" { Preset "none" } default { Write-Host "   nothing changed."; exit 0 } }
    if ($c -eq "4") { foreach ($it in $present) { $it.on = Ask "$($it.label)$(if ($it.size) { ' (' + (Fmt $it.size) + ')' })?" ($it.group -eq "app") } }
    if ($c -in @("1", "2") -and $items["memory"].present) {
        $items["memory"].on = Ask "Delete your memories and saved chats too?" $true
    }
}
$on = @{}; foreach ($it in $present) { $on[$it.key] = $it.on }
if (-not ($on.Values -contains $true)) { Write-Host "`n   nothing selected; nothing changed."; if (-not $Yes) { Read-Host "   press Enter to close" | Out-Null }; exit 0 }

Step "Will remove"
Show
$total = ($present | Where-Object { $_.on } | Measure-Object size -Sum).Sum
Write-Host "`n   frees about $(Fmt $total)" -ForegroundColor White
Write-Host "   Not touched: the NVIDIA driver, Microsoft Edge, the laptop and its PAIR app, any files of yours in this folder." -ForegroundColor DarkGray
if ($DryRun) { Write-Host "`n   dry run: nothing was changed."; exit 0 }
$export = $on["memory"] -and -not $NoExport
if ($on["memory"] -and -not $Yes -and -not $NoExport) { $export = Ask "Save a zip of your memories + chats to the desktop first?" $true }
if (-not $Yes) {
    $a = Read-Host "`n   type  uninstall  to go ahead (anything else cancels)"
    if ($a -ne "uninstall") { Write-Host "   cancelled; nothing changed."; exit 0 }
}

# ------------------------------------------------------------------ 1. stop everything that runs from here
Step "Stopping the command center, model servers and its window"
$procs = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and (Has $_.ExecutablePath ($Root + "\"))) -or
    (($_.Name -match "^pythonw?(\d|\.)*exe$|^py\.exe$") -and (Has $_.CommandLine $CC)) -or
    (($_.Name -eq "msedge.exe") -and (Has $_.CommandLine (Join-Path $CC "edge-profile"))) -or
    (($_.Name -eq "Code.exe") -and (Has $_.CommandLine (Join-Path $Root "vscode-laptop")))
} | Where-Object { $_.ProcessId -ne $PID })
foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
if ($procs.Count) { Start-Sleep -Seconds 2; Ok "stopped $($procs.Count) process(es)" } else { Ok "nothing was running" }

# ------------------------------------------------------------------ 2. phone access
if ($on["serve"] -and -not $on["tailscale"]) {
    Step "Phone access"
    & $ts serve --https=443 off 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "tailscale serve is off" } else { Note "couldn't turn tailscale serve off: run  tailscale serve --https=443 off" }
}

# ------------------------------------------------------------------ 3. main Cline back to its own provider
if ($on["cline"] -and -not $on["vscode"]) {
    Step "Cline"
    while (Get-Process Code -ErrorAction SilentlyContinue) {
        if ($Yes) { break }
        Note "VS Code is open; Cline would write its old settings back when it closes."
        if (-not (Ask "Close VS Code now, then press Enter (n = skip this step)" $true)) { break }
    }
    if (Get-Process Code -ErrorAction SilentlyContinue) { Note "skipped: VS Code is still open. Set a new provider in Cline's settings later." }
    elseif (-not $py) { Note "skipped (needs Python): set a new provider in Cline's settings." }
    else {
        # put back only the provider fields from the backup taken before the command center changed them
        $pyFile = Join-Path $env:TEMP "cc-uninstall-cline.py"
        @'
import json, os, sys
gs, bak = sys.argv[1], sys.argv[2]
g = json.load(open(gs, encoding="utf-8"))
b = json.load(open(bak, encoding="utf-8")) if os.path.exists(bak) else {}
keys = ("actModeApiProvider", "planModeApiProvider", "openAiBaseUrl", "actModeOpenAiModelId", "planModeOpenAiModelId")
for k in keys:
    if k in b: g[k] = b[k]
    else: g.pop(k, None) if k == "openAiBaseUrl" else None
url = g.get("openAiBaseUrl") or ""
tmp = gs + ".tmp"
json.dump(g, open(tmp, "w", encoding="utf-8"), indent=1); os.replace(tmp, gs)
pp = os.path.join(os.path.dirname(gs), "settings", "providers.json")
if os.path.exists(pp):
    p = json.load(open(pp, encoding="utf-8"))
    oc = ((p.get("providers") or {}).get("openai-compatible") or {}).get("settings")
    if oc is not None and "127.0.0.1:80" in (oc.get("baseUrl") or ""):
        if url: oc["baseUrl"] = url
        else: oc.pop("baseUrl", None)
        json.dump(p, open(pp + ".tmp", "w", encoding="utf-8"), indent=1); os.replace(pp + ".tmp", pp)
print(url or "(none)")
'@ | Set-Content -Encoding ascii $pyFile
        $r = & cmd /c "$py -I `"$pyFile`" `"$clineGs`" `"$clineBak`""
        Remove-Item -Force $pyFile -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -eq 0) {
            Ok "Cline's provider address is back to: $r"
            if ((Has $r "127.0.0.1:8090") -or (Has $r "127.0.0.1:8099")) { Note "that was this PC's model server, which is gone now: pick a new provider in Cline's settings" }
            Remove-Item -Force $clineBak -ErrorAction SilentlyContinue
        } else { Note "couldn't update Cline's settings; set a new provider in Cline yourself" }
    }
}

# ------------------------------------------------------------------ 4. memories + chats
if ($on["memory"]) {
    Step "Memories and saved chats"
    if ($export) {
        $zip = Join-Path ([Environment]::GetFolderPath("Desktop")) ("command-center-memory-" + (Get-Date -Format "yyyy-MM-dd-HHmm") + ".zip")
        $have = @($memPaths | Where-Object { Test-Path -LiteralPath $_ })
        try { Compress-Archive -LiteralPath $have -DestinationPath $zip -Force -ErrorAction Stop; Ok "saved $zip" }
        catch { Bad "couldn't make the zip ($($_.Exception.Message)); memories NOT deleted"; $on["memory"] = $false }
    }
    if ($on["memory"]) {
        $okAll = $true; foreach ($p in $memPaths) { if (-not (RemovePath $p)) { $okAll = $false } }
        if ((Test-Path -LiteralPath $memHome) -and -not (Get-ChildItem -LiteralPath $memHome -Force)) { Remove-Item -LiteralPath $memHome -Force }
        if ($okAll) { Ok "deleted" }
    }
}

# ------------------------------------------------------------------ 5. models + engine
if ($on["models"]) { Step "Models"; if (RemovePath (Join-Path $Root "models")) { Ok "deleted $(Fmt $items['models'].size)" } }
if ($on["engine"]) { Step "llama.cpp"; $ok = $true; foreach ($e in $engines) { if (-not (RemovePath $e)) { $ok = $false } }; if ($ok) { Ok "deleted $($engines.Count) build(s)" } }

# ------------------------------------------------------------------ 6. shortcuts
if ($on["shortcuts"]) {
    Step "Shortcuts"
    foreach ($s in $shortcuts) { if (RemovePath $s) { Ok "removed $s" } }
    Note "a taskbar pin can linger as a blank icon until you sign out; right-click it > Unpin if so"
}

# ------------------------------------------------------------------ 7. worktrees in your projects
if ($on["worktrees"]) {
    Step "Laptop worktrees"
    foreach ($w in $worktrees) {
        & $git -C $w.repo worktree remove $w.wt 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok "removed $($w.wt) (branch cline-laptop is kept in $($w.repo))" }
        else { Note "left $($w.wt): it has uncommitted changes (git -C `"$($w.repo)`" worktree remove --force `"$($w.wt)`" to drop them)" }
    }
}

# ------------------------------------------------------------------ 8. prerequisites
if ($on["pip"] -and $py) {
    Step "Python packages"
    & cmd /c "$py -m pip uninstall -y $($pyPkgs -join ' ') 2>nul" | Out-Null
    Ok "removed $($pyPkgs -join ', ') (their helper packages, e.g. httpx, stay; harmless)"
}
if ($on["vscode"]) {
    Step "VS Code + Cline"
    Get-Process Code -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $code = (Get-Command code -ErrorAction SilentlyContinue).Source
    if ($code) { & cmd /c "`"$code`" --uninstall-extension saoudrizwan.claude-dev 2>nul" | Out-Null; Ok "Cline extension removed" }
    if (RemovePath (Join-Path $env:USERPROFILE ".cline")) { Ok "deleted ~\.cline" }
    if ($hasCode) { WingetRemove "Microsoft.VisualStudioCode" }
    Note "VS Code's own settings stay in %APPDATA%\Code and ~\.vscode (delete them if you want it all gone)"
}
if ($on["tailscale"]) { Step "Tailscale"; WingetRemove "Tailscale.Tailscale" }
if ($on["git"])       { Step "Git"; WingetRemove "Git.Git" }
if ($on["python"])    { Step "Python"; foreach ($id in $pyIds) { WingetRemove $id } }
if (($on["tailscale"] -or $on["vscode"] -or $on["git"] -or $on["python"]) -and -not $Winget) {
    Note "winget isn't available: remove those in Settings > Apps > Installed apps"
}

# ------------------------------------------------------------------ 9. the program itself (last)
$wipeFolder = $false
if ($on["app"]) {
    Step "Command center program"
    $ok = $true; foreach ($p in $appPaths) { if (-not (RemovePath $p)) { $ok = $false } }
    if ($ok) { Ok "deleted" }
    $left = @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin @("uninstall.ps1", "uninstall.bat") })
    if ($left.Count) {
        Write-Host "`n   Still in $Root (not made by the command center, or kept):" -ForegroundColor White
        $left | Select-Object -First 15 | ForEach-Object { Write-Host "     $($_.Name)" }
        if ($left.Count -gt 15) { Write-Host "     ... and $($left.Count - 15) more" }
        $wipeFolder = Ask "Delete the whole folder, these included?" $false
    } else { $wipeFolder = $true }
}

Step "Done"
Write-Host "   You may want to restart the PC so Windows forgets the taskbar pin and any open handles."
if (-not $Yes) { Read-Host "   press Enter to close" | Out-Null }
if ($on["app"]) {
    # this script and uninstall.bat are still in use: a detached cmd deletes them (and the folder) once we've exited
    $rm = if ($wipeFolder) { "rmdir /s /q `"$Root`"" } else { "del /f /q `"$($selfPaths[0])`" `"$($selfPaths[1])`" & rmdir `"$Root`"" }
    Start-Process -WindowStyle Hidden -WorkingDirectory $env:TEMP cmd.exe -ArgumentList "/c ping -n 4 127.0.0.1 >nul & $rm"
}
