param(
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    [switch]$Recreate,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPath = Join-Path $root ".venv"
$runtimeRoot = Join-Path $root ".runtime"
$requirementsPath = Join-Path $root "requirements.lock.txt"

function Assert-WithinProject([string]$path) {
    $resolvedRoot = $root.TrimEnd('\') + '\'
    $resolvedPath = [System.IO.Path]::GetFullPath($path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $resolvedPath"
    }
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Base Python 3.13 executable was not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Dependency lock file was not found: $requirementsPath"
}

$pythonVersion = (& $PythonExe --version 2>&1 | Select-Object -First 1)
if ($pythonVersion -notmatch "Python 3\.13\.") {
    throw "Python 3.13 is required by this lock file; found: $pythonVersion"
}

if ($SelfTest) {
    [pscustomobject]@{
        Root = $root
        Python = $PythonExe
        PythonVersion = $pythonVersion
        Requirements = $requirementsPath
        ExistingVenv = (Test-Path -LiteralPath $venvPath -PathType Container)
        RecreateRequired = $true
    }
    exit 0
}

if ((Test-Path -LiteralPath $venvPath) -and -not $Recreate) {
    throw "The .venv directory already exists. Use -Recreate only when recovery is needed."
}

Assert-WithinProject $venvPath
Assert-WithinProject $runtimeRoot
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$previousPath = Join-Path $runtimeRoot "venv_previous_$stamp"
$failedPath = Join-Path $runtimeRoot "venv_failed_$stamp"

if (Test-Path -LiteralPath $venvPath) {
    Assert-WithinProject $previousPath
    Move-Item -LiteralPath $venvPath -Destination $previousPath
}

try {
    & $PythonExe -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "venv creation failed with exit code $LASTEXITCODE"
    }
    $venvPython = Join-Path $venvPath "Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip bootstrap failed with exit code $LASTEXITCODE"
    }
    & $venvPython -m pip install --requirement $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "dependency installation failed with exit code $LASTEXITCODE"
    }
    # web.app intentionally refuses to import without authentication settings.
    # Supply process-local test values when a recovery is being validated before
    # the machine-local .env file has been created. Existing operator values are
    # never overwritten, and these defaults disappear when this script exits.
    if ([string]::IsNullOrWhiteSpace($env:AUTH_USERNAME)) {
        $env:AUTH_USERNAME = "restore_test_viewer"
    }
    if ([string]::IsNullOrWhiteSpace($env:AUTH_PASSWORD_HASH)) {
        $env:AUTH_PASSWORD_HASH = "restore-test-only-hash"
    }
    if ([string]::IsNullOrWhiteSpace($env:APP_SECRET_KEY)) {
        $env:APP_SECRET_KEY = "restore-test-only-secret"
    }
    & $venvPython -m unittest discover -s (Join-Path $root "tests") -q
    if ($LASTEXITCODE -ne 0) {
        throw "restored environment failed the test suite with exit code $LASTEXITCODE"
    }
    [pscustomobject]@{
        Status = "completed"
        Venv = $venvPath
        PreviousVenv = $previousPath
        PythonVersion = $pythonVersion
        Requirements = $requirementsPath
    }
} catch {
    if (Test-Path -LiteralPath $venvPath) {
        Assert-WithinProject $failedPath
        Move-Item -LiteralPath $venvPath -Destination $failedPath
    }
    if (Test-Path -LiteralPath $previousPath) {
        Move-Item -LiteralPath $previousPath -Destination $venvPath
    }
    throw
}
