[CmdletBinding()]
param(
    [switch]$InstallIfMissing,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

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

function Test-PortOccupied {
    param([int]$Port)

    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        return $false
    }

    try {
        $null = Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop | Select-Object -First 1
        return $true
    }
    catch {
        return $false
    }
}

function Start-ServiceProcess {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string]$Command,
        [string]$StdOutLogPath,
        [string]$StdErrLogPath,
        [switch]$DryRunMode
    )

    $shellCommand = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($null -eq $shellCommand) {
        $shellCommand = Get-Command powershell -ErrorAction Stop
    }

    $escapedWorkingDirectory = $WorkingDirectory.Replace("'", "''")
    $launchCommand = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$escapedWorkingDirectory'; $Command"

    if ($DryRunMode) {
        Write-Host "DRY RUN => $($shellCommand.Source) -WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -Command $launchCommand"
        Write-Host "DRY RUN => stdout: $StdOutLogPath"
        Write-Host "DRY RUN => stderr: $StdErrLogPath"
        return
    }

    $process = Start-Process -FilePath $shellCommand.Source -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        $launchCommand
    ) -RedirectStandardOutput $StdOutLogPath -RedirectStandardError $StdErrLogPath

    Write-Host "$Title started in background (launcher PID: $($process.Id))"
}

Require-Command node
Require-Command npm
Require-Command uv

$envPath = Join-Path $repoRoot '.env'
$envExamplePath = Join-Path $repoRoot '.env.example'

if (-not (Test-Path $envPath)) {
    if (-not (Test-Path $envExamplePath)) {
        throw '.env.example not found. Cannot create .env automatically.'
    }

    Copy-Item $envExamplePath $envPath
    Write-Warning '.env was created from .env.example. Fill in the required API keys and run the launcher again.'
    exit 0
}

$needsNodeInstall = -not (Test-Path (Join-Path $repoRoot 'node_modules')) -or -not (Test-Path (Join-Path $repoRoot 'frontend\node_modules'))
$needsBackendInstall = -not (Test-Path (Join-Path $repoRoot 'backend\.venv'))

if ($InstallIfMissing -or $needsNodeInstall) {
    if ($DryRun) {
        Write-Host 'DRY RUN => npm install'
        Write-Host 'DRY RUN => cd frontend && npm install'
    }
    else {
        Write-Host 'Installing Node dependencies...'
        npm install
        Push-Location (Join-Path $repoRoot 'frontend')
        try {
            npm install
        }
        finally {
            Pop-Location
        }
    }
}

if ($InstallIfMissing -or $needsBackendInstall) {
    if ($DryRun) {
        Write-Host 'DRY RUN => cd backend && uv sync'
    }
    else {
        Write-Host 'Installing backend dependencies...'
        Push-Location (Join-Path $repoRoot 'backend')
        try {
            uv sync
        }
        finally {
            Pop-Location
        }
    }
}

$frontendPort = Read-EnvValue -Path $envPath -Key 'FRONTEND_PORT' -DefaultValue '3001'
$backendPort = Read-EnvValue -Path $envPath -Key 'FLASK_PORT' -DefaultValue '5001'

if (Test-PortOccupied -Port ([int]$frontendPort)) {
    Write-Warning "Port $frontendPort is already in use. Frontend startup may fail."
}

if (Test-PortOccupied -Port ([int]$backendPort)) {
    Write-Warning "Port $backendPort is already in use. Backend startup may fail."
}

$backendCommand = 'uv run python run.py'
$frontendCommand = 'npm run dev'

$logDirectory = Join-Path $repoRoot 'backend\logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

$backendStdOutLogPath = Join-Path $logDirectory 'mirofish-backend.stdout.log'
$backendStdErrLogPath = Join-Path $logDirectory 'mirofish-backend.stderr.log'
$frontendStdOutLogPath = Join-Path $logDirectory 'mirofish-frontend.stdout.log'
$frontendStdErrLogPath = Join-Path $logDirectory 'mirofish-frontend.stderr.log'

Start-ServiceProcess -Title 'MiroFish Backend' -WorkingDirectory (Join-Path $repoRoot 'backend') -Command $backendCommand -StdOutLogPath $backendStdOutLogPath -StdErrLogPath $backendStdErrLogPath -DryRunMode:$DryRun
Start-ServiceProcess -Title 'MiroFish Frontend' -WorkingDirectory (Join-Path $repoRoot 'frontend') -Command $frontendCommand -StdOutLogPath $frontendStdOutLogPath -StdErrLogPath $frontendStdErrLogPath -DryRunMode:$DryRun

Write-Host ''
Write-Host 'MiroFish services are configured for background start.'
Write-Host "Frontend: http://localhost:$frontendPort"
Write-Host "Backend:  http://localhost:$backendPort"
Write-Host "Logs:     $logDirectory"
Write-Host 'Use stop-mirofish.bat to stop the services.'
