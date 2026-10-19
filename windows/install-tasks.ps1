<#
  install-tasks.ps1 -- register the Jarvis scheduled tasks for the CURRENT user (no admin needed).

    powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1                 # register
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1 -RestartAt 05:30
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1 -BootReport     # + the boot report
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1 -WhatIf         # show, change nothing
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1 -Remove         # unregister all

  Jarvis-Dispatcher          at logon (+45 s)  -> run_hidden.vbs -> dispatcher.ps1 (the supervisor, foreground)
                             The task INSTANCE is the supervisor's lifetime, so MultipleInstances=IgnoreNew
                             guarantees one dispatcher and Stop-ScheduledTask ends the whole tree. No time
                             limit; restarted up to 3 times if it fails to launch.
  Jarvis-Dispatcher-Restart  daily at -RestartAt (06:45) -> task-restart.ps1: a fresh context every morning.
  Jarvis-Dispatcher-Heal     every 15 min at :05 :20 :35 :50 -> task-heal.ps1: starts the dispatcher if its
                             supervisor is gone; silent otherwise.
  Jarvis-Boot-Report         (-BootReport) at logon (+3 min) -> always-on\boot-report.ps1: proof of what came
                             up by itself after a reboot.

  "Run only when the user is logged on" is DELIBERATE: the Claude login, the bot poller, Docker and any
  cloud-drive mount are per-user, session-bound things. A service or a SYSTEM task cannot see them. The
  machine signs itself in instead (windows\always-on\README.md). Nothing here shows a window.
#>
param([string]$RestartAt = '06:45', [switch]$BootReport, [switch]$Remove, [switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$here = (Get-Item -LiteralPath $PSScriptRoot).FullName
$names = @('Jarvis-Dispatcher', 'Jarvis-Dispatcher-Restart', 'Jarvis-Dispatcher-Heal', 'Jarvis-Boot-Report')

if ($Remove) {
  foreach ($n in $names) {
    if ($WhatIf) { Write-Host "would unregister $n"; continue }
    Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "unregistered $n"
  }
  exit 0
}

$vbs = Join-Path $here 'run_hidden.vbs'
foreach ($p in @($vbs, (Join-Path $here 'dispatcher.ps1'), (Join-Path $here 'task-restart.ps1'), (Join-Path $here 'task-heal.ps1'))) {
  if (-not (Test-Path -LiteralPath $p)) { throw "MISSING: $p" }
}
$who = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
function Action($script) {
  New-ScheduledTaskAction -Execute 'wscript.exe' -WorkingDirectory (Split-Path -Parent $here) `
    -Argument ('//B //Nologo "{0}" "{1}"' -f $vbs, (Join-Path $here $script))
}
function Register($name, $action, $trigger, $settings, $what) {
  if ($WhatIf) { Write-Host "would register '$name' - $what"; return }
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  Write-Host "registered '$name' - $what"
}

# --- the dispatcher: at logon, lives as long as the supervisor --------------------------------------------
$t = New-ScheduledTaskTrigger -AtLogOn -User $who
$t.Delay = 'PT45S'   # let the desktop, the network and the PATH settle after sign-in
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
Register 'Jarvis-Dispatcher' (Action 'dispatcher.ps1') $t $s 'at logon (+45 s), single instance, no time limit'

# --- the daily restart: a fresh context -----------------------------------------------------------------------
$at = [datetime]::ParseExact($RestartAt, 'HH:mm', $null)
$first = (Get-Date).Date.AddHours($at.Hour).AddMinutes($at.Minute)
if ($first -le (Get-Date)) { $first = $first.AddDays(1) }
$t = New-ScheduledTaskTrigger -Daily -At $first
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register 'Jarvis-Dispatcher-Restart' (Action 'task-restart.ps1') $t $s "daily at $RestartAt"

# --- the heal: every 15 minutes, offset from the quarter hours ----------------------------------------------
# no -RepetitionDuration = indefinitely (Task Scheduler rejects [TimeSpan]::MaxValue)
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 15)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register 'Jarvis-Dispatcher-Heal' (Action 'task-heal.ps1') $t $s 'every 15 minutes (:05 :20 :35 :50), starts the dispatcher only if it is gone'

# --- optional: the boot report --------------------------------------------------------------------------------
if ($BootReport) {
  $t = New-ScheduledTaskTrigger -AtLogOn -User $who
  $t.Delay = 'PT3M'
  $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
  Register 'Jarvis-Boot-Report' (Action 'always-on\boot-report.ps1') $t $s 'at logon (+3 min): logs\boot-report.log'
}

Write-Host ''
Write-Host "  start now:   Start-ScheduledTask -TaskName 'Jarvis-Dispatcher'"
Write-Host "  restart now: Start-ScheduledTask -TaskName 'Jarvis-Dispatcher-Restart'"
Write-Host "  state:       python -m jarvis dispatcher --status"
