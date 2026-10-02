$ErrorActionPreference = "Stop"

$taskName = "IEEE_Paper_Server_WeeklyUpdate"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$batchPath = Join-Path $PSScriptRoot "update_ieee_weekly.bat"

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "IEEE weekly batch file was not found: $batchPath"
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
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
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
    -Description "IEEE Crossref metadata collection, validation and exact-run import; no publisher login or PDF downloads" `
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
