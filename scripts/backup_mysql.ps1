param(
    [switch]$SelfTest,
    [switch]$NoPrune
)

$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$backupRoot = if ($env:MYSQL_BACKUP_DEST) {
    [System.IO.Path]::GetFullPath($env:MYSQL_BACKUP_DEST)
} else {
    Join-Path $root "_backups\mysql_daily"
}
$retentionDays = 7
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archiveName = "mysql_$stamp.tar.gz"
$archivePath = Join-Path $backupRoot $archiveName
$partialPath = "$archivePath.partial"
$checksumPath = "$archivePath.sha256"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "IEEE_Paper_Server_mysql_${stamp}_$PID"
$optionFile = Join-Path ([System.IO.Path]::GetTempPath()) "IEEE_Paper_Server_mysql_client_${stamp}_$PID.cnf"
$tar = (Get-Command tar.exe -ErrorAction Stop).Source

function Read-DotEnv([string]$path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split(@("="), 2, [System.StringSplitOptions]::None)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Resolve-MySqlTool([string]$name) {
    $candidates = @()
    if ($env:MYSQL_BIN) {
        $candidates += (Join-Path $env:MYSQL_BIN "$name.exe")
    }
    if ($env:MYSQL_BASE) {
        $candidates += (Join-Path $env:MYSQL_BASE "bin\$name.exe")
    }
    $candidates += (Join-Path $env:USERPROFILE "mysql\mysql-8.4.9-winx64\bin\$name.exe")
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    $command = Get-Command "$name.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw "MySQL tool was not found: $name.exe"
}

function Escape-OptionValue([string]$value) {
    if ($value -match "[\r\n]") {
        throw "MySQL option values must not contain line breaks."
    }
    return $value.Replace("\", "\\").Replace('"', '\"')
}

$envPath = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Database configuration file is missing: $envPath"
}
$config = Read-DotEnv $envPath
$requiredKeys = @("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
foreach ($key in $requiredKeys) {
    if (-not $config.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($config[$key])) {
        throw "Required database setting is missing: $key"
    }
}
if ($config.DB_NAME -notmatch "^[A-Za-z0-9_]+$") {
    throw "DB_NAME contains unsupported characters."
}

$mysqldump = Resolve-MySqlTool "mysqldump"
$mysql = Resolve-MySqlTool "mysql"

if ($SelfTest) {
    [pscustomobject]@{
        Root = $root
        BackupRoot = $backupRoot
        RetentionDays = $retentionDays
        Database = $config.DB_NAME
        MySqlDump = $mysqldump
        MySql = $mysql
        Tar = $tar
        CredentialsLogged = $false
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null

    $optionLines = @(
        "[client]",
        ('host="{0}"' -f (Escape-OptionValue $config.DB_HOST)),
        ('port="{0}"' -f (Escape-OptionValue $config.DB_PORT)),
        ('user="{0}"' -f (Escape-OptionValue $config.DB_USER)),
        ('password="{0}"' -f (Escape-OptionValue $config.DB_PASSWORD)),
        'default-character-set="utf8mb4"'
    )
    $optionLines | Set-Content -LiteralPath $optionFile -Encoding ASCII
    $defaultsArgument = "--defaults-extra-file=$optionFile"

    $sqlName = "$($config.DB_NAME).sql"
    $sqlPath = Join-Path $staging $sqlName
    $dumpErrorPath = Join-Path $staging "mysqldump.stderr.txt"
    & $mysqldump $defaultsArgument `
        --single-transaction `
        --quick `
        --routines `
        --events `
        --triggers `
        --hex-blob `
        --default-character-set=utf8mb4 `
        --no-tablespaces `
        --set-gtid-purged=OFF `
        --databases $config.DB_NAME `
        "--result-file=$sqlPath" 2> $dumpErrorPath
    if ($LASTEXITCODE -ne 0) {
        $diagnostic = (Get-Content -LiteralPath $dumpErrorPath -Raw -ErrorAction SilentlyContinue).Trim()
        throw "mysqldump failed with exit code $LASTEXITCODE. $diagnostic"
    }
    if (-not (Test-Path -LiteralPath $sqlPath -PathType Leaf) -or
        (Get-Item -LiteralPath $sqlPath).Length -le 0) {
        throw "mysqldump completed without producing a non-empty SQL file."
    }

    $queryErrorPath = Join-Path $staging "mysql.stderr.txt"
    $rowQuery = "SELECT COUNT(*) FROM ``$($config.DB_NAME)``.papers;"
    $rowOutput = & $mysql $defaultsArgument --batch --skip-column-names "--execute=$rowQuery" 2> $queryErrorPath
    if ($LASTEXITCODE -ne 0) {
        $diagnostic = (Get-Content -LiteralPath $queryErrorPath -Raw -ErrorAction SilentlyContinue).Trim()
        throw "MySQL validation query failed with exit code $LASTEXITCODE. $diagnostic"
    }
    $paperRows = [long]($rowOutput | Select-Object -First 1)

    Remove-Item -LiteralPath $optionFile -Force
    if ((Test-Path -LiteralPath $dumpErrorPath) -and (Get-Item -LiteralPath $dumpErrorPath).Length -eq 0) {
        Remove-Item -LiteralPath $dumpErrorPath -Force
    }
    if ((Test-Path -LiteralPath $queryErrorPath) -and (Get-Item -LiteralPath $queryErrorPath).Length -eq 0) {
        Remove-Item -LiteralPath $queryErrorPath -Force
    }

    $sqlHash = (Get-FileHash -LiteralPath $sqlPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$sqlHash  $sqlName" | Set-Content -LiteralPath (Join-Path $staging "SHA256SUMS.txt") -Encoding ASCII
    $dumpVersion = (& $mysqldump --version | Select-Object -First 1)
    $manifest = [ordered]@{
        created_at = (Get-Date).ToString("o")
        computer = $env:COMPUTERNAME
        database = $config.DB_NAME
        paper_rows = $paperRows
        sql_file = $sqlName
        sql_bytes = (Get-Item -LiteralPath $sqlPath).Length
        sql_sha256 = $sqlHash
        retention_days = $retentionDays
        dump_tool = $dumpVersion
        restore_hint = "mysql --defaults-extra-file=<client.cnf> < $sqlName"
    }
    $manifest | ConvertTo-Json -Depth 3 | Set-Content `
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
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$archiveHash  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ASCII

    $expired = @()
    if (-not $NoPrune) {
        $cutoff = (Get-Date).AddDays(-$retentionDays)
        $expired = Get-ChildItem -LiteralPath $backupRoot -File -Filter "mysql_*.tar.gz" |
            Where-Object { $_.LastWriteTime -lt $cutoff }
        foreach ($file in $expired) {
            Remove-Item -LiteralPath $file.FullName -Force
            $oldChecksum = "$($file.FullName).sha256"
            if (Test-Path -LiteralPath $oldChecksum -PathType Leaf) {
                Remove-Item -LiteralPath $oldChecksum -Force
            }
        }
    }

    [pscustomobject]@{
        Status = "completed"
        Archive = $archivePath
        Checksum = $checksumPath
        PaperRows = $paperRows
        Bytes = (Get-Item -LiteralPath $archivePath).Length
        PruningDisabled = [bool]$NoPrune
        RemovedExpired = $expired.Count
    }
} finally {
    if (Test-Path -LiteralPath $optionFile) {
        Remove-Item -LiteralPath $optionFile -Force
    }
    if (Test-Path -LiteralPath $partialPath) {
        Remove-Item -LiteralPath $partialPath -Force
    }
    if (Test-Path -LiteralPath $staging) {
        $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $resolvedStaging = [System.IO.Path]::GetFullPath($staging)
        if (-not $resolvedStaging.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove staging directory outside the temp root: $resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
