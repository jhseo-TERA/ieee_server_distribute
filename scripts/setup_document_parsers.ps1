param(
    [Parameter(Mandatory = $true)][string]$BasePython,
    [switch]$DownloadModels,
    [string]$SamplePdf = "ieee-pdf/11509585.pdf"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot ".runtime/document-parsers"
$parserPython = Join-Path $runtimePath "Scripts/python.exe"
Push-Location $projectRoot
try {
    & $BasePython -c "import sys; assert sys.version_info[:2] == (3,12), 'Use Python 3.12 for the validated parser runtime'"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }
    if (-not (Test-Path -LiteralPath $parserPython)) {
        & $BasePython -m venv $runtimePath
        if ($LASTEXITCODE -ne 0) { throw "Could not create parser runtime." }
    }
    # Older pip resolvers reject PaddleX's self-referencing extra incorrectly.
    & $parserPython -m pip install --disable-pip-version-check --no-input --cache-dir .runtime/pip-cache --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not update pip." }
    $requirements = Join-Path $PSScriptRoot "requirements_document_parsers.lock.txt"
    if (-not (Test-Path -LiteralPath $requirements)) {
        $requirements = Join-Path $PSScriptRoot "requirements_document_parsers.txt"
    }
    & $parserPython -m pip install --disable-pip-version-check --no-input --cache-dir .runtime/pip-cache -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "Parser dependency installation failed." }
    & $parserPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Parser dependency validation failed." }
    if ($DownloadModels) {
        $samplePath = (Resolve-Path -LiteralPath $SamplePdf).Path
        & $parserPython scripts/paddle_layout_worker.py --pdf $samplePath --output outputs/document_parsers/paddle_setup.json --max-pages 1 --download-model
        if ($LASTEXITCODE -ne 0) { throw "Paddle model preparation failed." }
        & $parserPython scripts/mineru_worker.py --download-models
        if ($LASTEXITCODE -ne 0) { throw "MinerU model preparation failed." }
        Write-Host "PaddleOCR and MinerU local models are ready."
    }
    & $parserPython scripts/document_parser_status.py
    if ($LASTEXITCODE -ne 0) { throw "Parser runtime status failed." }
} finally {
    Pop-Location
}
