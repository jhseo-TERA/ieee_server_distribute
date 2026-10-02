$ErrorActionPreference = 'Stop'
$expected = @(
    'IEEE_Paper_Server_WeeklyUpdate',
    'IEEE_Paper_Server_NatureWeeklyUpdate',
    'IEEE_Paper_Server_OpticaWeeklyUpdate'
)
try {
    $allTasks = @(Get-ScheduledTask -ErrorAction Stop)
} catch {
    throw "Cannot inspect Task Scheduler: $($_.Exception.Message). Access denied is NOT evidence that tasks are absent. Run this read-only inspection with an authorized Windows session."
}
$matching = @($allTasks | Where-Object {
    $_.TaskName -in $expected -or ($_.Actions.Arguments -join ' ') -match 'update_(ieee|nature|optica)_weekly'
})
$results = @($matching | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo -ErrorAction Stop
    [pscustomobject]@{
        name = $_.TaskName
        path = $_.TaskPath
        state = [string]$_.State
        user = $_.Principal.UserId
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
