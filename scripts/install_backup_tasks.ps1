$ErrorActionPreference = "Stop"

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$definitions = @(
    [pscustomobject]@{
        TaskName = "IEEE_Paper_Server_DailySourceBackup"
        Batch = "backup_source.bat"
        At = "02:00"
        Hours = 1
        Description = "Private local source snapshot; PDFs and project data excluded; seven-day retention"
    },
    [pscustomobject]@{
        TaskName = "IEEE_Paper_Server_DailyMySQLBackup"
        Batch = "backup_mysql.bat"
        At = "02:15"
        Hours = 2
        Description = "Private local MySQL logical backup with checksum and seven-day retention"
    }
)

$registered = foreach ($definition in $definitions) {
    $batchPath = Join-Path $PSScriptRoot $definition.Batch
    if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
        throw "Backup batch file was not found: $batchPath"
    }

    $arguments = '/d /c ""{0}""' -f $batchPath
    $action = New-ScheduledTaskAction `
        -Execute $cmdPath `
        -Argument $arguments `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $definition.At
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours $definition.Hours) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    Register-ScheduledTask `
        -TaskName $definition.TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $definition.Description `
        -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $definition.TaskName
    $info = Get-ScheduledTaskInfo -TaskName $definition.TaskName
    [pscustomobject]@{
        TaskName = $task.TaskName
        State = $task.State
        NextRunTime = $info.NextRunTime
        Execute = $task.Actions.Execute
        Arguments = $task.Actions.Arguments
        WorkingDirectory = $task.Actions.WorkingDirectory
    }
}

$registered
