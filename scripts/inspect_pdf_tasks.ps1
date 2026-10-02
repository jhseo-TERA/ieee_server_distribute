$ErrorActionPreference = 'Stop'
$expected = @(
    'IEEE_Paper_Server_OpticaMorningPDFDownload',
    'IEEE_Paper_Server_DailyPDFIntegrity',
    'IEEE_Paper_Server_DailyPDFDownload'
)
try {
    $allTasks = @(Get-ScheduledTask -ErrorAction Stop)
} catch {
    throw "Task Scheduler inspection failed: $($_.Exception.Message). Access denied does not mean tasks are missing."
}
$matching = @($allTasks | Where-Object {
    $_.TaskName -in $expected -or ($_.Actions.Arguments -join ' ') -match 'run_pdf_(download|integrity)_daily'
})
$results = @($matching | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo -ErrorAction Stop
    [pscustomobject]@{
        name = $_.TaskName
        state = [string]$_.State
        user = $_.Principal.UserId
        logonType = [string]$_.Principal.LogonType
        lastRun = $info.LastRunTime
        lastResult = $info.LastTaskResult
        nextRun = $info.NextRunTime
        command = $_.Actions.Execute
        arguments = $_.Actions.Arguments
        workingDirectory = $_.Actions.WorkingDirectory
        startWhenAvailable = $_.Settings.StartWhenAvailable
        multipleInstances = [string]$_.Settings.MultipleInstances
    }
})
[pscustomobject]@{
    tasks = $results
    missing = @($expected | Where-Object { $_ -notin $matching.TaskName })
} | ConvertTo-Json -Depth 4
