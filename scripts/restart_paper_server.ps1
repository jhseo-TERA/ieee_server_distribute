param([Parameter(Mandatory=$true)][int]$ExpectedPid, [string]$InterruptRecommendationJobId = '')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root '.venv\Scripts\python.exe'
$listeners = @(Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction Stop)
if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or $listeners[0].OwningProcess -ne $ExpectedPid) {
    throw 'Server listener changed; inspect the current process before retrying.'
}
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$ExpectedPid"
$serverArguments = ' -m waitress --host=127.0.0.1 --port=5001 web.app:app'
$expectedCommands = @(('"' + $python + '"' + $serverArguments), ($python + $serverArguments))
if (-not $process -or $process.CommandLine.Trim() -notin $expectedCommands) {
    throw 'Refusing to restart an unverified process.'
}
Push-Location $root
try {
    $preflight = @'
import sys
from sqlalchemy import text
from web.app import engine

with engine.connect() as connection:
    active = list(connection.execute(
        text('SELECT id,kind,owner FROM ai_jobs WHERE status IN (:queued,:running)'),
        {'queued': 'queued', 'running': 'running'},
    ))
allowed = sys.argv[1] if len(sys.argv) > 1 else ''
safe = not active or (
    len(active) == 1
    and tuple(active[0]) == (allowed, 'recommendations', '__recommendation_worker__')
)
raise SystemExit(0 if safe else 1)
'@
    & $python -c $preflight $InterruptRecommendationJobId
    if ($LASTEXITCODE -ne 0) { throw 'AI work is active or preflight failed; wait before restarting.' }
} finally { Pop-Location }
$processPath = $env:Path
[Environment]::SetEnvironmentVariable('PATH',$null,'Process')
[Environment]::SetEnvironmentVariable('Path',$processPath,'Process')
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $root "logs\paper-server-recommendation-$stamp.out.log"
$stderr = Join-Path $root "logs\paper-server-recommendation-$stamp.err.log"
Stop-Process -Id $ExpectedPid -ErrorAction Stop
Wait-Process -Id $ExpectedPid -Timeout 10 -ErrorAction SilentlyContinue
$launched = Start-Process -FilePath $python -ArgumentList @('-m','waitress','--host=127.0.0.1','--port=5001','web.app:app') `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$ready = $false
for ($attempt=0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:5001/health' -TimeoutSec 2
        if ($health.ok) { $ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) { throw "Restarted server did not become healthy. Inspect $stderr" }
$newListeners = @(Get-NetTCPConnection -LocalPort 5001 -State Listen)
if ($newListeners.Count -ne 1) { throw 'Expected exactly one server listener after restart.' }
$newPid = $newListeners[0].OwningProcess
Set-Content -LiteralPath (Join-Path $root '.runtime\paper-server.pid') -Value $newPid
[PSCustomObject]@{old_pid=$ExpectedPid;new_pid=$newPid;healthy=$ready;stderr=$stderr;stdout=$stdout} | ConvertTo-Json
