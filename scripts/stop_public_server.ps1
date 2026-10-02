$root = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $root ".runtime"

foreach ($name in @("cloudflared.pid", "paper-server.pid")) {
    $pidFile = Join-Path $runtimeDir $name
    if (-not (Test-Path $pidFile)) { continue }
    $processId = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if ($processId) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $processId
            Write-Host "Stopped $($process.ProcessName) (PID $processId)"
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

Remove-Item (Join-Path $runtimeDir "public-url.txt") -Force -ErrorAction SilentlyContinue
