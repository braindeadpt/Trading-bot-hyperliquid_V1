# Stop only Hyperliquid bot processes for THIS project folder.
# Does NOT kill other Python bots/scripts on the same machine.
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Continue"
$root = (Resolve-Path $ProjectRoot).Path.TrimEnd('\', '/')
$rootLower = $root.ToLower()
$folderName = Split-Path $root -Leaf
$folderLower = $folderName.ToLower()

$lockPath = Join-Path $root 'data\live\bot.lock'
$targetPids = [System.Collections.Generic.HashSet[int]]::new()
$allProcs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)

function Test-BotCommandLine {
    param([string]$CommandLine)
    if (-not $CommandLine) { return $false }
    $cmdLower = $CommandLine.ToLower()
    if ($cmdLower -notmatch 'main\.py' -and $cmdLower -notmatch 'run_with_recovery\.py') {
        return $false
    }
    if ($cmdLower.Contains($rootLower)) { return $true }
    if ($cmdLower.Contains($folderLower) -and $cmdLower.Contains('main.py')) { return $true }
    return $false
}

function Test-BotProcess {
    param($Proc)
    if (-not $Proc) { return $false }
    if ($Proc.Name -notmatch 'python') { return $false }
    return (Test-BotCommandLine $Proc.CommandLine)
}

function Add-TargetPid {
    param([int]$ProcId)
    if ($ProcId -gt 0) { [void]$targetPids.Add($ProcId) }
}

function Add-ProcessTree {
    param([int]$SeedPid)
    if ($SeedPid -le 0) { return }
    Add-TargetPid $SeedPid

    # Descendants (pythoncore children, etc.)
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($SeedPid)
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in $allProcs) {
            if ($child.ParentProcessId -eq $parentId) {
                $childId = [int]$child.ProcessId
                if ($targetPids.Add($childId)) {
                    $queue.Enqueue($childId)
                }
            }
        }
    }

    # Ancestors: python launcher / cmd hosting this bot
    $current = $allProcs | Where-Object { $_.ProcessId -eq $SeedPid } | Select-Object -First 1
    $hops = 0
    while ($current -and $current.ParentProcessId -gt 0 -and $hops -lt 6) {
        $parent = $allProcs | Where-Object { $_.ProcessId -eq $current.ParentProcessId } | Select-Object -First 1
        if (-not $parent) { break }
        $parentName = ($parent.Name + '').ToLower()
        if ($parentName -match 'python' -and (Test-BotCommandLine $parent.CommandLine)) {
            Add-TargetPid ([int]$parent.ProcessId)
        }
        elseif ($parentName -eq 'cmd.exe') {
            $pcmd = ($parent.CommandLine + '').ToLower()
            if ($pcmd.Contains($folderLower) -or $pcmd.Contains('quickstart.bat') -or $pcmd.Contains('service.bat') -or $pcmd.Contains('start.bat')) {
                Add-TargetPid ([int]$parent.ProcessId)
            }
        }
        $current = $parent
        $hops++
    }
}

# 1) PID from single-instance lock
if (Test-Path $lockPath) {
    $lockPid = 0
    [void][int]::TryParse((Get-Content $lockPath -Raw).Trim(), [ref]$lockPid)
    if ($lockPid -gt 0) {
        Add-ProcessTree $lockPid
    }
}

# 2) Any python process whose command line references this project's main.py
foreach ($proc in $allProcs) {
    if (Test-BotProcess $proc) {
        Add-ProcessTree ([int]$proc.ProcessId)
    }
}

# 3) Fallback: process listening on dashboard port 5000 (if it is our bot)
try {
    $listeners = @(Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue)
    foreach ($conn in $listeners) {
        $ownerId = [int]$conn.OwningProcess
        $owner = $allProcs | Where-Object { $_.ProcessId -eq $ownerId } | Select-Object -First 1
        if ($owner -and ($owner.Name -match 'python') -and (Test-BotProcess $owner)) {
            Add-ProcessTree $ownerId
        }
    }
} catch {
    # netstat fallback when Get-NetTCPConnection is slow/unavailable
    $netstat = netstat -ano 2>$null | Select-String ':5000\s+.*LISTENING\s+(\d+)$'
    foreach ($line in $netstat) {
        if ($line -match 'LISTENING\s+(\d+)\s*$') {
            $ownerId = [int]$Matches[1]
            $owner = $allProcs | Where-Object { $_.ProcessId -eq $ownerId } | Select-Object -First 1
            if ($owner -and ($owner.Name -match 'python') -and (Test-BotProcess $owner)) {
                Add-ProcessTree $ownerId
            }
        }
    }
}

if ($targetPids.Count -eq 0) {
    Write-Host "No bot instances found for this project."
    Write-Host "  $root"
    if (Test-Path $lockPath) {
        Remove-Item -Force $lockPath -ErrorAction SilentlyContinue
        Write-Host "Cleared stale lock: $lockPath"
    }
    exit 0
}

Write-Host "Found $($targetPids.Count) process(es) to stop:"
foreach ($procId in ($targetPids | Sort-Object)) {
    $p = $allProcs | Where-Object { $_.ProcessId -eq $procId } | Select-Object -First 1
    if ($p) {
        Write-Host "  PID $procId [$($p.Name)] $($p.CommandLine)"
    } else {
        Write-Host "  PID $procId - (already exited)"
    }
}

$stopped = 0
foreach ($procId in $targetPids) {
    $result = & taskkill /PID $procId /F /T 2>&1
    if ($LASTEXITCODE -eq 0) {
        $stopped++
    } else {
        $still = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $still) {
            $stopped++
        } else {
            Write-Host "  WARN: could not stop PID $procId - $result"
        }
    }
}

if (Test-Path $lockPath) {
    Remove-Item -Force $lockPath -ErrorAction SilentlyContinue
    Write-Host "Cleared instance lock: $lockPath"
}

Write-Host "Stopped $stopped / $($targetPids.Count) process(es)."
Write-Host "Other Python processes on this PC were NOT touched."
