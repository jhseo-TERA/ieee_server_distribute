$ErrorActionPreference = "Stop"

# Some Codex/Windows shells contain both Path and PATH. Start-Process treats
# those as duplicate dictionary keys, so normalize them before launching.
$processPath = $env:Path
[Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[Environment]::SetEnvironmentVariable("Path", $processPath, "Process")

$root = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $root ".runtime"
$logDir = Join-Path $root "logs"
$python = Join-Path $root ".venv\Scripts\python.exe"
$cloudflared = Join-Path $root "tools\cloudflared.exe"
$serverPidFile = Join-Path $runtimeDir "paper-server.pid"
$tunnelPidFile = Join-Path $runtimeDir "cloudflared.pid"
$tunnelLog = Join-Path $logDir ("cloudflared-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$urlFile = Join-Path $runtimeDir "public-url.txt"

function Get-LocalServerListenerPids {
    param([int]$Port)
    $pattern = "^\s*TCP\s+127\.0\.0\.1:$Port\s+0\.0\.0\.0:0\s+LISTENING\s+(\d+)\s*$"
    return @(
        netstat.exe -ano -p tcp | ForEach-Object {
            if ($_ -match $pattern) { [int]$Matches[1] }
        } | Sort-Object -Unique
    )
}

New-Item -ItemType Directory -Force $runtimeDir, $logDir | Out-Null

if (-not (Test-Path $python)) {
    throw "Python environment is missing: $python"
}
if (-not (Test-Path $cloudflared)) {
    throw "cloudflared is missing: $cloudflared"
}

& cmd.exe /c (Join-Path $PSScriptRoot "start_mysql.bat")
if ($LASTEXITCODE -ne 0) {
    throw "MySQL did not start successfully (exit code $LASTEXITCODE)."
}

& $python (Join-Path $PSScriptRoot "serdes_data_pipeline.py") --migrate-only
if ($LASTEXITCODE -ne 0) {
    throw "SerDes schema migration failed (exit code $LASTEXITCODE)."
}

$listenerPids = @(Get-LocalServerListenerPids -Port 5001)
if ($listenerPids.Count -gt 1) {
    throw "Multiple processes are listening on 127.0.0.1:5001: $($listenerPids -join ', ')"
}
if ($listenerPids.Count -eq 0) {
    # Avoid the generated waitress-serve.exe shim: on Windows it can leave an
    # untracked Python child bound to port 5001 after the shim PID is stopped.
    $server = Start-Process -FilePath $python -ArgumentList @(
        "-m", "waitress", "--host=127.0.0.1", "--port=5001", "web.app:app"
    ) -WorkingDirectory $root -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logDir "paper-server.out.log") `
      -RedirectStandardError (Join-Path $logDir "paper-server.err.log")
    Set-Content -Path $serverPidFile -Value $server.Id
} else {
    Set-Content -Path $serverPidFile -Value $listenerPids[0]
}

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:5001/health" -TimeoutSec 2
        if ($health.ok) { $ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) {
    throw "Paper server did not become ready. Check logs\paper-server.err.log"
}

$activeListenerPids = @(Get-LocalServerListenerPids -Port 5001)
if ($activeListenerPids.Count -ne 1) {
    throw "Expected one listener on 127.0.0.1:5001, found $($activeListenerPids.Count)."
}
Set-Content -Path $serverPidFile -Value $activeListenerPids[0]

if (Test-Path $tunnelPidFile) {
    $oldPid = [int](Get-Content $tunnelPidFile -ErrorAction SilentlyContinue)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $oldPid -Force
    }
}
Remove-Item $urlFile -Force -ErrorAction SilentlyContinue

$tunnel = Start-Process -FilePath $cloudflared -ArgumentList @(
    "tunnel", "--url", "http://127.0.0.1:5001", "--no-autoupdate",
    "--protocol", "http2", "--logfile", $tunnelLog, "--loglevel", "info"
) -WorkingDirectory $root -WindowStyle Hidden -PassThru
Set-Content -Path $tunnelPidFile -Value $tunnel.Id

$publicUrl = $null
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path $tunnelLog) {
        $match = Select-String -Path $tunnelLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -AllMatches
        if ($match) {
            $urls = @($match | ForEach-Object { $_.Matches | ForEach-Object { $_.Value } })
            $publicUrl = $urls[-1]
            break
        }
    }
    if (-not (Get-Process -Id $tunnel.Id -ErrorAction SilentlyContinue)) {
        throw "Cloudflare Tunnel exited. Check logs\cloudflared.log"
    }
    Start-Sleep -Seconds 1
}
if (-not $publicUrl) {
    throw "Public URL was not issued within 60 seconds. Check logs\cloudflared.log"
}

Set-Content -Path $urlFile -Value $publicUrl
Write-Host ""
Write-Host "Public HTTPS URL: $publicUrl" -ForegroundColor Green
Write-Host "Login is required. Favorites are stored in the shared MySQL database."
Write-Host "Stop: scripts\stop_public_server.ps1"
