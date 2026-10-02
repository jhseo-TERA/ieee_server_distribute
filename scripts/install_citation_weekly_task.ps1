$ErrorActionPreference = "Stop"

$taskName = "IEEE_Paper_Server_CitationWeeklyUpdate"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$batchPath = Join-Path $PSScriptRoot "run_citation_weekly.bat"

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "Weekly citation batch file was not found: $batchPath"
}

$cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$arguments = '/d /c ""{0}""' -f $batchPath
$action = New-ScheduledTaskAction `
    -Execute $cmdPath `
    -Argument $arguments `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "04:00"
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
    -Description "Weekly IEEE/Crossref citation refresh followed by SQL-to-Zotero citation sync" `
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
