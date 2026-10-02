param(
    [switch]$Public
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$logDir = Join-Path $root "logs"
$startupLog = Join-Path $logDir "server-startup.log"
$launcherName = if ($Public) { "start_public_server.ps1" } else { "start_local_server.ps1" }
$launcher = Join-Path $PSScriptRoot $launcherName
$mode = if ($Public) { "public" } else { "local-only" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-StartupLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $startupLog -Value "[$timestamp] $Message" -Encoding UTF8
}

try {
    Write-StartupLog "Automatic server startup requested (mode: $mode)."
    & $launcher | Out-Null
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Server launcher returned exit code $LASTEXITCODE."
    }
    Write-StartupLog "Automatic server startup completed (mode: $mode)."
    exit 0
} catch {
    Write-StartupLog ("Automatic server startup failed: " + $_.Exception.Message)
    exit 1
}
