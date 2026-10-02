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

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment is missing: $python"
}

# Local-only mode must not reactivate a project-owned tunnel left over from a
# previous manual public-server run. Verify both PID and executable path before
# stopping anything so a stale PID file can never terminate an unrelated app.
if (Test-Path -LiteralPath $tunnelPidFile -PathType Leaf) {
    $savedTunnelPid = 0
    [void][int]::TryParse(
        (Get-Content -LiteralPath $tunnelPidFile -ErrorAction SilentlyContinue),
        [ref]$savedTunnelPid
    )
    $tunnelProcess = if ($savedTunnelPid) {
        Get-Process -Id $savedTunnelPid -ErrorAction SilentlyContinue
    } else {
        $null
    }
    if ($tunnelProcess -and
        $tunnelProcess.Path -and
        [string]::Equals(
            [System.IO.Path]::GetFullPath($tunnelProcess.Path),
            [System.IO.Path]::GetFullPath($cloudflared),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        Stop-Process -Id $savedTunnelPid -Force
    }
    Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $urlFile -Force -ErrorAction SilentlyContinue

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
    # Launch the module through Python itself. The generated waitress-serve.exe
    # shim can spawn an untracked child on Windows, leaving an old server bound
    # to port 5001 after the shim PID is stopped.
    $server = Start-Process -FilePath $python -ArgumentList @(
        "-m", "waitress", "--host=127.0.0.1", "--port=5001", "web.app:app"
    ) -WorkingDirectory $root -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logDir "paper-server.out.log") `
      -RedirectStandardError (Join-Path $logDir "paper-server.err.log")
    Set-Content -LiteralPath $serverPidFile -Value $server.Id
} else {
    Set-Content -LiteralPath $serverPidFile -Value $listenerPids[0]
}

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:5001/health" -TimeoutSec 2
        if ($health.ok) {
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) {
    throw "Paper server did not become ready. Check logs\paper-server.err.log"
}

# The Windows venv launcher may delegate to a child Python process. Record the
# process that actually owns the socket, not the short-lived launcher PID.
$activeListenerPids = @(Get-LocalServerListenerPids -Port 5001)
if ($activeListenerPids.Count -ne 1) {
    throw "Expected one listener on 127.0.0.1:5001, found $($activeListenerPids.Count)."
}
Set-Content -LiteralPath $serverPidFile -Value $activeListenerPids[0]

Write-Host "Local server: http://127.0.0.1:5001"
