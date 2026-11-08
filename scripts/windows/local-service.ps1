param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Start','Stop','Status','Remove')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath $Root).Path
$instancePath = Join-Path $taskRoot 'instance.json'
$instance = Get-Content -LiteralPath $instancePath -Raw | ConvertFrom-Json
if ($instance.schema_version -ne 'd07-local-v1' -or $instance.instance_id -notmatch '^[0-9a-f]{32}$') { throw 'Invalid local instance' }
if ([IO.Path]::GetFullPath($instance.data_root) -ne [IO.Path]::GetFullPath($taskRoot)) { throw 'Instance root mismatch' }
$projectRoot = (Resolve-Path -LiteralPath $instance.project_root).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonwPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $pythonwPath)) { throw 'Project venv missing' }
$taskName = 'MediaTag-Local-' + $instance.instance_id.Substring(0,12)
$description = 'media-tag current-user instance ' + $instance.instance_id + '; ' + $taskRoot
$arguments = '-m media_tag local-run --root "' + $taskRoot + '"'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Description -ne $description -or $existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pythonwPath -or $existing.Actions[0].Arguments -ne $arguments) { throw 'Refusing to modify a mismatched existing task' }
}
if ($Action -eq 'Install') {
    if (-not $existing) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        if ($instance.windows_sid -and $identity.User.Value -ne $instance.windows_sid) { throw 'Install must use the instance owner identity' }
        $taskAction = New-ScheduledTaskAction -Execute $pythonwPath -Argument $arguments -WorkingDirectory $projectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description $description | Out-Null
    }
} elseif ($Action -eq 'Start') {
    if (-not $existing) { throw 'Install task first' }
    Start-ScheduledTask -TaskName $taskName
} elseif ($Action -in @('Stop','Remove')) {
    & $pythonPath -m media_tag local-stop --root $taskRoot
    if ($LASTEXITCODE -ne 0) { throw 'Graceful stop failed; inspect local-status before changing task' }
    if ($existing -and $Action -eq 'Remove') { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{TaskName=$taskName; State=[string]$task.State; LastTaskResult=$info.LastTaskResult; Execute=$task.Actions[0].Execute; RunLevel=[string]$task.Principal.RunLevel; LogonType=[string]$task.Principal.LogonType} | ConvertTo-Json -Compress
} else { [pscustomobject]@{TaskName=$taskName;State='NotInstalled'} | ConvertTo-Json -Compress }
