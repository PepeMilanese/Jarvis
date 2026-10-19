<#
  boot-report.ps1 -- proof of what came up BY ITSELF after a reboot. Run by the optional Jarvis-Boot-Report
  task 3 minutes after sign-in (install-tasks.ps1 -BootReport); appends to logs\boot-report.log.

  The number that matters is "minutes since boot": a report written ~3 minutes after power-on, in the
  console session, can only have come from auto sign-in - nobody was there to type a password.
  Writes no user name, machine name or address.
#>
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$state = if ($env:JARVIS_STATE) { $env:JARVIS_STATE } else { $root }
$logs = Join-Path $state 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log = Join-Path $logs 'boot-report.log'
function W($m) { Add-Content -Path $log -Value $m -Encoding utf8 }

$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
W ''
W ('=== boot report {0:yyyy-MM-dd HH:mm:ss}' -f (Get-Date))
W ('last boot          : {0:yyyy-MM-dd HH:mm:ss}' -f $boot)
W ('minutes since boot : {0:N1}' -f ((Get-Date) - $boot).TotalMinutes)
W ('session            : {0}  (1 = the console session: auto sign-in, not a remote logon)' -f (Get-Process -Id $PID).SessionId)
$dirty = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 41, 6008 } -MaxEvents 5 | Where-Object { $_.TimeCreated -gt $boot.AddMinutes(-15) }
W ('shutdown before it : {0}' -f $(if ($dirty) { 'UNCLEAN (event 41/6008 - a power cut or a hard reset)' } else { 'clean' }))

$wl = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
W ('AutoAdminLogon     : {0}' -f $wl.AutoAdminLogon)
W ('HiberbootEnabled   : {0}  (want 0)' -f (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power').HiberbootEnabled)
W ('WU NoAutoUpdate    : {0}' -f (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU').NoAutoUpdate)

$sup = 'not running'
$pidFile = Join-Path $logs 'dispatcher.pid'
if (Test-Path -LiteralPath $pidFile) {
  $rec = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
  $p = Get-Process -Id ([int]$rec.pid)
  if ($p -and (-not $rec.created -or [math]::Abs((([DateTimeOffset]$p.StartTime).ToUnixTimeMilliseconds() / 1000.0) - [double]$rec.created) -le 2)) { $sup = 'running' }
}
W ('Jarvis supervisor  : {0}' -f $sup)
foreach ($n in @('Jarvis-Dispatcher', 'Jarvis-Dispatcher-Heal')) {
  $t = Get-ScheduledTask -TaskName $n
  if ($t) { W ('{0,-19}: {1}, last result {2}' -f $n, $t.State, ($t | Get-ScheduledTaskInfo).LastTaskResult) }
}
$ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (Test-Path -LiteralPath $ts) {
  $st = & $ts status --json 2>$null | ConvertFrom-Json
  W ('Tailscale          : {0}, online {1}, unattended {2}' -f $st.BackendState, $st.Self.Online, (& $ts debug prefs 2>$null | ConvertFrom-Json).ForceDaemon)
}
W '==='
