# task-restart.ps1 -- the daily fresh-context restart, run hidden by the Jarvis-Dispatcher-Restart task.
# Also fine to run by hand.
#
# Order matters: stop the supervisor and the bot poller FIRST, so the old session cannot answer a
# message in the seconds between the two task operations; then stop the task instance; then start it.
# Running jobs are not touched: workers belong to their own watchdogs, and the new dispatcher's first
# `status` picks them up again. The durable schedule (schedule.json) carries every watch poll and timed
# prompt across the restart.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$state = if ($env:JARVIS_STATE) { $env:JARVIS_STATE } else { $root }
$log = Join-Path $state 'logs\dispatcher.log'
function Log($m) { Add-Content -Path $log -Value ("[{0}] restart-task: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8 }

$py = $env:JARVIS_PYTHON
if (-not $py) {
  $c = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $c) { $c = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 }
  $py = $c.Source
}
Log 'begin'
Push-Location -LiteralPath $root
& $py -m jarvis dispatcher --stop 2>&1 | ForEach-Object { Log "$_" }
Pop-Location
Stop-ScheduledTask -TaskName 'Jarvis-Dispatcher' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5
Start-ScheduledTask -TaskName 'Jarvis-Dispatcher'
Start-Sleep -Seconds 20
$state = (Get-ScheduledTask -TaskName 'Jarvis-Dispatcher' -ErrorAction SilentlyContinue).State
Log "done: task state=$state"
if ($state -ne 'Running') { exit 1 }
exit 0
