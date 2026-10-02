param(
    [switch]$InitializeFromClipboard
)

$ErrorActionPreference = "Stop"

$cloudflaredHome = Join-Path $env:USERPROFILE "cloudflared"
$cloudflaredExe = Join-Path $cloudflaredHome "cloudflared.exe"
$tokenFile = Join-Path $cloudflaredHome "paper_repo_token.dpapi"
$logDirectory = Join-Path $cloudflaredHome "logs"
$stdoutLog = Join-Path $logDirectory "paper_repo.stdout.log"
$stderrLog = Join-Path $logDirectory "paper_repo.stderr.log"

if (-not (Test-Path -LiteralPath $cloudflaredExe -PathType Leaf)) {
    throw "cloudflared.exe not found: $cloudflaredExe"
}

if ($InitializeFromClipboard) {
    $clipboard = Get-Clipboard -Raw -ErrorAction Stop
    if ($clipboard -notmatch '^cloudflared(?:\.exe)? service install\s+(?<token>\S+)\s*$') {
        throw "Cloudflare service-install command was not found in the clipboard."
    }

    if (-not (Test-Path -LiteralPath $cloudflaredHome -PathType Container)) {
        New-Item -ItemType Directory -Path $cloudflaredHome | Out-Null
    }

    $secureToken = ConvertTo-SecureString $Matches.token -AsPlainText -Force
    $secureToken | ConvertFrom-SecureString | Set-Content -LiteralPath $tokenFile -Encoding ascii
    & icacls.exe $tokenFile /inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null

    $clipboard = $null
    $secureToken = $null
}

if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
    throw "Encrypted tunnel token not found. Re-run with -InitializeFromClipboard."
}

$existing = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
    Where-Object { $_.ExecutablePath -eq $cloudflaredExe -and $_.CommandLine -match '\btunnel\b' }
if ($existing) {
    Write-Output "cloudflared is already running (PID $($existing[0].ProcessId))."
    exit 0
}

if (-not (Test-Path -LiteralPath $logDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $logDirectory | Out-Null
}

$encryptedToken = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
$secureToken = ConvertTo-SecureString $encryptedToken
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    $process = Start-Process `
        -FilePath $cloudflaredExe `
        -ArgumentList @("tunnel", "--no-autoupdate", "run", "--token", $plainToken) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
} finally {
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
    $plainToken = $null
    $secureToken = $null
    $encryptedToken = $null
}

Start-Sleep -Seconds 3
if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
    throw "cloudflared exited during startup. Check $stderrLog"
}

Write-Output "cloudflared started (PID $($process.Id))."
Write-Output "Logs: $logDirectory"
