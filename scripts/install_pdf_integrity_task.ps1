$ErrorActionPreference = "Stop"

$taskName = "IEEE_Paper_Server_DailyPDFIntegrity"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$batchPath = Join-Path $PSScriptRoot "run_pdf_integrity_daily.bat"

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "PDF integrity batch file was not found: $batchPath"
}

$cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$arguments = '/d /c ""{0}""' -f $batchPath
$action = New-ScheduledTaskAction `
    -Execute $cmdPath `
    -Argument $arguments `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At "15:30"
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
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
    -Description "Full PDF integrity survey; queues damaged or missing files before the 19:00 daily repair/download job" `
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
