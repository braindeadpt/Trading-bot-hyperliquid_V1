# Stop only Hyperliquid bot processes for THIS project folder.
# Does NOT kill other Python bots/scripts on the same machine.
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "SilentlyContinue"
$root = (Resolve-Path $ProjectRoot).Path.TrimEnd('\', '/')
$rootLower = $root.ToLower()

$entryScripts = @(
    [regex]::Escape($root) + [regex]::Escape('\') + 'main\.py',
    [regex]::Escape($root) + [regex]::Escape('/') + 'main\.py',
    [regex]::Escape($root) + [regex]::Escape('\') + 'run_with_recovery\.py',
    [regex]::Escape($root) + [regex]::Escape('/') + 'run_with_recovery\.py'
)

function Test-BotCommandLine {
    param([string]$CommandLine)
    if (-not $CommandLine) { return $false }
    $cmdLower = $CommandLine.ToLower()
    if ($cmdLower -notmatch 'main\.py' -and $cmdLower -notmatch 'run_with_recovery\.py') {
        return $false
    }
    foreach ($pat in $entryScripts) {
        if ($CommandLine -match $pat) { return $true }
    }
    return $false
}

$targetPids = [System.Collections.Generic.HashSet[int]]::new()

# 1) PID from single-instance lock (this project's bot.lock)
$lockPath = Join-Path $root 'data\live\bot.lock'
if (Test-Path $lockPath) {
    $lockPid = 0
    [void][int]::TryParse((Get-Content $lockPath -Raw).Trim(), [ref]$lockPid)
    if ($lockPid -gt 0) {
        [void]$targetPids.Add($lockPid)
    }
}

# 2) Any python process whose command line references this project's main.py / recovery wrapper
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' } |
    ForEach-Object {
        if (Test-BotCommandLine $_.CommandLine) {
            [void]$targetPids.Add([int]$_.ProcessId)
        }
    }

if ($targetPids.Count -eq 0) {
    Write-Host "No bot instances found for this project."
    Write-Host "  $root"
    exit 0
}

Write-Host "Found $($targetPids.Count) instance(s) to stop:"
foreach ($procId in $targetPids) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
    if ($p) {
        Write-Host "  PID $procId - $($p.CommandLine)"
    } else {
        Write-Host "  PID $procId - (process already exited)"
    }
}

foreach ($procId in $targetPids) {
    # /T kills child pythoncore when WindowsApps shim is parent
    & taskkill /PID $procId /F /T 2>$null | Out-Null
}

Write-Host "Stopped $($targetPids.Count) instance(s)."
Write-Host "Other Python processes on this PC were NOT touched."
