# task-heal.ps1 -- the 15-minute self-heal, run hidden by the Jarvis-Dispatcher-Heal task.
#
# If the supervisor is not running, start the Jarvis-Dispatcher task (whose instance IS the supervisor's
# lifetime; MultipleInstances=IgnoreNew makes this idempotent). If it is running, do nothing and log
# nothing - a quiet check must not fill the log 96 times a day.
#
# Why it exists: the dispatcher task fires at logon and the restart task once a day, so a supervisor that
# died at 01:27 stayed dead until the morning - five hours of "the bot doesn't answer". It runs at
# :05 :20 :35 :50, never on the quarter hours next to the daily restart: once it fired in the same second
# as the restart, saw the supervisor the restart had just stopped, and raced it to start the task.
#
# "Running" means the pid in dispatcher.pid is alive AND started when the file says it did: a pid file
# left behind by a crash or a power cut can name a pid Windows has since given to something else.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$state = if ($env:JARVIS_STATE) { $env:JARVIS_STATE } else { $root }   # where Jarvis keeps logs\ (as in Python)
$log = Join-Path $state 'logs\dispatcher.log'
$pidFile = Join-Path $state 'logs\dispatcher.pid'
function Log($m) { Add-Content -Path $log -Value ("[{0}] heal-task: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8 }

$alive = $false
if (Test-Path -LiteralPath $pidFile) {
  try {
    $rec = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $p = Get-Process -Id ([int]$rec.pid) -ErrorAction Stop
    $alive = $true
    if ($rec.created) {
      $started = ([DateTimeOffset]$p.StartTime).ToUnixTimeMilliseconds() / 1000.0
      if ([math]::Abs($started - [double]$rec.created) -gt 2) { $alive = $false }   # a reused pid
    }
  } catch { }
}
if ($alive) { exit 0 }

Log 'supervisor not running - starting the Jarvis-Dispatcher task'
$t = Get-ScheduledTask -TaskName 'Jarvis-Dispatcher' -ErrorAction SilentlyContinue
if (-not $t) { Log 'Jarvis-Dispatcher is not registered (run windows\install-tasks.ps1)'; exit 1 }
if ($t.State -eq 'Disabled') { exit 0 }   # switched off on purpose
if ($t.State -eq 'Running') {
  # the task instance is alive but the supervisor is not: a dead wrapper. Reset it.
  Log 'task instance still Running without a supervisor - stopping it first'
  Stop-ScheduledTask -TaskName 'Jarvis-Dispatcher' -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 5
}
Start-ScheduledTask -TaskName 'Jarvis-Dispatcher'
Start-Sleep -Seconds 25
Log ("done: task state={0}" -f (Get-ScheduledTask -TaskName 'Jarvis-Dispatcher' -ErrorAction SilentlyContinue).State)
exit 0
