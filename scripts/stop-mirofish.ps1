[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $repoRoot '.env'
$launcherTitles = @('MiroFish Backend', 'MiroFish Frontend')

function Read-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$DefaultValue
    )

    if (-not (Test-Path $Path)) {
        return $DefaultValue
    }

    $pattern = "^\s*$([regex]::Escape($Key))\s*=\s*(.+?)\s*$"
    $match = Select-String -Path $Path -Pattern $pattern | Select-Object -First 1
    if ($null -eq $match) {
        return $DefaultValue
    }

    return $match.Matches[0].Groups[1].Value.Trim()
}

function Get-ProcessSnapshot {
    Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine
}

function Get-LauncherRoots {
    param([object[]]$ProcessSnapshot)

    $ProcessSnapshot | Where-Object {
        $commandLine = $_.CommandLine
        if (-not $commandLine) {
            return $false
        }

        foreach ($title in $launcherTitles) {
            if ($commandLine -like "*$title*" -and $commandLine -like "*$repoRoot*") {
                return $true
            }
        }

        return $false
    }
}

function Get-DescendantProcessInfo {
    param(
        [object[]]$ProcessSnapshot,
        [object[]]$RootProcesses
    )

    if (-not $RootProcesses) {
        return @()
    }

    $childrenByParent = @{}
    foreach ($process in $ProcessSnapshot) {
        $parentKey = [string]$process.ParentProcessId
        if (-not $childrenByParent.ContainsKey($parentKey)) {
            $childrenByParent[$parentKey] = @()
        }

        $childrenByParent[$parentKey] += $process
    }

    $queue = [System.Collections.Generic.Queue[object]]::new()
    foreach ($rootProcess in $RootProcesses) {
        $queue.Enqueue([pscustomobject]@{
            Process = $rootProcess
            Depth = 0
            Reason = 'launcher-root'
        })
    }

    $visited = @{}
    $results = @()

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        $process = $current.Process
        $processId = [int]$process.ProcessId

        if ($visited.ContainsKey($processId)) {
            continue
        }

        $visited[$processId] = $true
        $results += [pscustomobject]@{
            ProcessId = $processId
            ParentProcessId = [int]$process.ParentProcessId
            Name = $process.Name
            CommandLine = $process.CommandLine
            Depth = [int]$current.Depth
            Reasons = @($current.Reason)
        }

        $childKey = [string]$processId
        if ($childrenByParent.ContainsKey($childKey)) {
            foreach ($childProcess in $childrenByParent[$childKey]) {
                $queue.Enqueue([pscustomobject]@{
                    Process = $childProcess
                    Depth = [int]$current.Depth + 1
                    Reason = 'launcher-descendant'
                })
            }
        }
    }

    return $results
}

function Get-PortProcessInfo {
    param(
        [object[]]$ProcessSnapshot,
        [int[]]$Ports
    )

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return @()
    }

    $processById = @{}
    foreach ($process in $ProcessSnapshot) {
        $processById[[int]$process.ProcessId] = $process
    }

    $results = @()
    foreach ($port in $Ports) {
        try {
            $connections = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop)
        }
        catch {
            $connections = @()
        }

        foreach ($connection in $connections) {
            $processId = [int]$connection.OwningProcess
            if ($processById.ContainsKey($processId)) {
                $process = $processById[$processId]
                $name = $process.Name
                $parentProcessId = [int]$process.ParentProcessId
                $commandLine = $process.CommandLine
            }
            else {
                $name = "PID-$processId"
                $parentProcessId = -1
                $commandLine = ''
            }

            $results += [pscustomobject]@{
                ProcessId = $processId
                ParentProcessId = $parentProcessId
                Name = $name
                CommandLine = $commandLine
                Depth = 0
                Reasons = @("listening-port:$port")
            }
        }
    }

    return $results
}

function Merge-TargetProcesses {
    param([object[]]$Candidates)

    $merged = @{}
    foreach ($candidate in $Candidates) {
        $processId = [int]$candidate.ProcessId
        if ($processId -eq $PID) {
            continue
        }

        if (-not $merged.ContainsKey($processId)) {
            $merged[$processId] = [pscustomobject]@{
                ProcessId = $processId
                ParentProcessId = [int]$candidate.ParentProcessId
                Name = $candidate.Name
                CommandLine = $candidate.CommandLine
                Depth = [int]$candidate.Depth
                Reasons = @($candidate.Reasons)
            }
            continue
        }

        $existing = $merged[$processId]
        $existing.Depth = [Math]::Max([int]$existing.Depth, [int]$candidate.Depth)
        $existing.Reasons = @($existing.Reasons + $candidate.Reasons | Select-Object -Unique)

        if ([string]::IsNullOrWhiteSpace($existing.CommandLine) -and -not [string]::IsNullOrWhiteSpace($candidate.CommandLine)) {
            $existing.CommandLine = $candidate.CommandLine
        }
    }

    return @(
        $merged.Values | Sort-Object -Property @(
            @{ Expression = 'Depth'; Descending = $true },
            @{ Expression = 'ProcessId'; Descending = $true }
        )
    )
}

function Format-CommandPreview {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return ''
    }

    $preview = $CommandLine.Trim()
    if ($preview.Length -gt 140) {
        return $preview.Substring(0, 140) + '...'
    }

    return $preview
}

$frontendPort = [int](Read-EnvValue -Path $envPath -Key 'FRONTEND_PORT' -DefaultValue '3001')
$backendPort = [int](Read-EnvValue -Path $envPath -Key 'FLASK_PORT' -DefaultValue '5001')

$processSnapshot = @(Get-ProcessSnapshot)
$launcherRoots = @(Get-LauncherRoots -ProcessSnapshot $processSnapshot)
$descendantProcesses = @(Get-DescendantProcessInfo -ProcessSnapshot $processSnapshot -RootProcesses $launcherRoots)
$portProcesses = @(Get-PortProcessInfo -ProcessSnapshot $processSnapshot -Ports @($frontendPort, $backendPort))
$targets = @(Merge-TargetProcesses -Candidates @($descendantProcesses + $portProcesses))

if (-not $targets -or $targets.Count -eq 0) {
    Write-Host 'No running MiroFish frontend/backend processes were found.'
    exit 0
}

Write-Host 'Matched MiroFish processes:'
foreach ($target in $targets) {
    $reasonText = ($target.Reasons | Select-Object -Unique) -join ', '
    $commandPreview = Format-CommandPreview -CommandLine $target.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandPreview)) {
        Write-Host ("- PID {0} [{1}] Reasons: {2}" -f $target.ProcessId, $target.Name, $reasonText)
    }
    else {
        Write-Host ("- PID {0} [{1}] Reasons: {2} | {3}" -f $target.ProcessId, $target.Name, $reasonText, $commandPreview)
    }
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'DRY RUN only. No processes were stopped.'
    exit 0
}

$stoppedProcessIds = @()
foreach ($target in $targets) {
    try {
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        $stoppedProcessIds += $target.ProcessId
    }
    catch {
        $message = $_.Exception.Message
        if ($message -notlike '*Cannot find a process*') {
            Write-Warning ("Failed to stop PID {0}: {1}" -f $target.ProcessId, $message)
        }
    }
}

$stoppedProcessIds = @($stoppedProcessIds | Select-Object -Unique)
if ($stoppedProcessIds.Count -gt 0) {
    Wait-Process -Id $stoppedProcessIds -Timeout 5 -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'MiroFish stop sequence completed.'
