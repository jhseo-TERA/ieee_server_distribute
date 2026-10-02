$ErrorActionPreference = "Stop"

$morningTaskName = "IEEE_Paper_Server_OpticaMorningPDFDownload"
$afternoonTaskName = "IEEE_Paper_Server_DailyPDFDownload"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$batchPath = Join-Path $PSScriptRoot "run_pdf_download_daily.bat"

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "PDF 일간 배치 파일을 찾을 수 없습니다: $batchPath"
}

$cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"
$morningArguments = '/d /c ""{0}" --provider optica --limit 13"' -f $batchPath
$afternoonArguments = '/d /c ""{0}""' -f $batchPath
$morningAction = New-ScheduledTaskAction `
    -Execute $cmdPath `
    -Argument $morningArguments `
    -WorkingDirectory $root
$afternoonAction = New-ScheduledTaskAction `
    -Execute $cmdPath `
    -Argument $afternoonArguments `
    -WorkingDirectory $root
$morningTrigger = New-ScheduledTaskTrigger -Daily -At "09:00"
$afternoonTrigger = New-ScheduledTaskTrigger -Daily -At "19:00"
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

@(
    [pscustomobject]@{
        TaskName = $morningTaskName
        Action = $morningAction
        Trigger = $morningTrigger
        Description = "IEEE Paper Server Optica 오전 PDF 확보(최대 13건; IEEE 제공 JLT 별도 큐)"
    }
    [pscustomobject]@{
        TaskName = $afternoonTaskName
        Action = $afternoonAction
        Trigger = $afternoonTrigger
        Description = "IEEE Paper Server 즐겨찾기 PDF 저녁 확보(IEEE/JLT/Nature 및 Optica 최대 8건, 25~40분 랜덤 간격)"
    }
) | ForEach-Object {
    Register-ScheduledTask `
        -TaskName $_.TaskName `
        -Action $_.Action `
        -Trigger $_.Trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $_.Description `
        -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $_.TaskName
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
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
}
