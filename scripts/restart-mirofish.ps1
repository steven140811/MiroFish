[CmdletBinding()]
param(
    [switch]$InstallIfMissing,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$stopScript = Join-Path $PSScriptRoot 'stop-mirofish.ps1'
$startScript = Join-Path $PSScriptRoot 'start-mirofish.ps1'

if (-not (Test-Path $stopScript)) {
    throw 'stop-mirofish.ps1 not found.'
}

if (-not (Test-Path $startScript)) {
    throw 'start-mirofish.ps1 not found.'
}

& $stopScript -DryRun:$DryRun
& $startScript -InstallIfMissing:$InstallIfMissing -DryRun:$DryRun
