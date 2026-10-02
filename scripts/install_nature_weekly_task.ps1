$ErrorActionPreference = "Stop"

$taskName = "IEEE_Paper_Server_NatureWeeklyUpdate"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$batchPath = Join-Path $PSScriptRoot "update_nature_weekly.bat"

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "Nature weekly batch file was not found: $batchPath"
}

$cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$arguments = '/d /c ""{0}""' -f $batchPath
$action = New-ScheduledTaskAction `
    -Execute $cmdPath `
    -Argument $arguments `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "03:00"
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Weekly Nature Crossref metadata refresh and MySQL import; no publisher PDF downloads" `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    NextRunTime = $info.NextRunTime
    Execute = $task.Actions.Execute
    Arguments = $task.Actions.Arguments
    WorkingDirectory = $task.Actions.WorkingDirectory
    MultipleInstances = $task.Settings.MultipleInstances
    StartWhenAvailable = $task.Settings.StartWhenAvailable
    ExecutionTimeLimit = $task.Settings.ExecutionTimeLimit
}
