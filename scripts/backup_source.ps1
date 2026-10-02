param(
    [switch]$SelfTest,
    [switch]$NoPrune
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$backupRoot = if ($env:SOURCE_BACKUP_DEST) {
    [System.IO.Path]::GetFullPath($env:SOURCE_BACKUP_DEST)
} else {
    Join-Path $root "_backups\source_daily"
}
$retentionDays = 7
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archiveName = "source_$stamp.tar.gz"
$archivePath = Join-Path $backupRoot $archiveName
$partialPath = "$archivePath.partial"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "IEEE_Paper_Server_source_${stamp}_$PID"
$tar = (Get-Command tar.exe -ErrorAction Stop).Source

$sourceRoots = @("scripts", "web", "tests", "py_00_src", "config")
$rootFiles = @(
    "README_SERVER.md",
    "DAILY_WORKFLOW.md",
    "METADATA_UPDATE.md",
    "PDF_DOWNLOAD_ROUTINE.md",
    "BACKUP_POLICY.md",
    "RECOVERY_RUNBOOK.md",
    "LOCAL_AI_RESEARCH.md",
    "CLAUDE_CODE_지시문.md",
    "README.txt",
    "requirements.lock.txt",
    ".env.example",
    ".gitignore",
    ".gitattributes"
)
# Root-level Python modules contain the SerDes parsers, classifiers and family
# logic used by both Flask and recovery scripts; keep them in source backups.
$rootFiles += @(Get-ChildItem -LiteralPath $root -File -Filter "*.py" | Select-Object -ExpandProperty Name)

foreach ($relative in $sourceRoots) {
    $path = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Source directory is missing: $path"
    }
}

if ($SelfTest) {
    [pscustomobject]@{
        Root = $root
        BackupRoot = $backupRoot
        RetentionDays = $retentionDays
        Tar = $tar
        SourceRoots = $sourceRoots -join ", "
        Excludes = ".env, PDFs, data, logs, reports, runtimes, caches"
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    $copied = 0

    foreach ($relativeRoot in $sourceRoots) {
        $sourceBase = Join-Path $root $relativeRoot
        Get-ChildItem -LiteralPath $sourceBase -Recurse -File | Where-Object {
            $_.Extension.ToLowerInvariant() -notin @(
                ".pdf", ".xlsx", ".xls", ".csv", ".tsv", ".db", ".sqlite",
                ".zip", ".tar", ".gz", ".7z", ".exe", ".dll", ".pyc"
            ) -and
            $_.FullName -notmatch "[\\/]__pycache__[\\/]" -and
            $_.FullName -notmatch "[\\/]\.pytest_cache[\\/]" -and
            $_.Name -ne ".zotero_tag_ingest_state.json"
        } | ForEach-Object {
            $rootPrefix = $root.TrimEnd('\') + '\'
            if (-not $_.FullName.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to copy a file outside the project root: $($_.FullName)"
            }
            $relativePath = $_.FullName.Substring($rootPrefix.Length)
            $destination = Join-Path $staging $relativePath
            New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination
            $copied++
        }
    }

    foreach ($relativeFile in $rootFiles) {
        $source = Join-Path $root $relativeFile
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $staging $relativeFile)
            $copied++
        }
    }

    $manifest = [ordered]@{
        created_at = (Get-Date).ToString("o")
        computer = $env:COMPUTERNAME
        root = $root
        file_count = $copied
        retention_days = $retentionDays
        included_roots = $sourceRoots
        excluded = @(
            ".env", "*.pdf", "py_01_data", "py_02_reports", "logs",
            "_backups", ".venv", ".runtime", ".edge_profile_optica",
            "DesignCon", "Obsidian", "outputs", "pdf-quarantine"
        )
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content `
        -LiteralPath (Join-Path $staging "BACKUP_MANIFEST.json") -Encoding UTF8

    if (Test-Path -LiteralPath $partialPath) {
        Remove-Item -LiteralPath $partialPath -Force
    }
    & $tar -czf $partialPath -C $staging .
    if ($LASTEXITCODE -ne 0) {
        throw "tar creation failed with exit code $LASTEXITCODE"
    }
    & $tar -tzf $partialPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "tar verification failed with exit code $LASTEXITCODE"
    }
    Move-Item -LiteralPath $partialPath -Destination $archivePath

    $expired = @()
    if (-not $NoPrune) {
        $cutoff = (Get-Date).AddDays(-$retentionDays)
        $expired = Get-ChildItem -LiteralPath $backupRoot -File -Filter "source_*.tar.gz" |
            Where-Object { $_.LastWriteTime -lt $cutoff }
        foreach ($file in $expired) {
            Remove-Item -LiteralPath $file.FullName -Force
        }
    }

    [pscustomobject]@{
        Status = "completed"
        Archive = $archivePath
        Files = $copied
        Bytes = (Get-Item -LiteralPath $archivePath).Length
        PruningDisabled = [bool]$NoPrune
        RemovedExpired = $expired.Count
    }
} finally {
    if (Test-Path -LiteralPath $staging) {
        $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $resolvedStaging = [System.IO.Path]::GetFullPath($staging)
        if (-not $resolvedStaging.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove staging directory outside the temp root: $resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
