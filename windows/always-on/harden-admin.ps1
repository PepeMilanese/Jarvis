<#
  harden-admin.ps1 -- the always-on settings that need ADMINISTRATOR rights. Self-elevates (one UAC click).
  Idempotent; every change is printed. -WhatIf shows the plan without elevating or changing anything.

    powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-admin.ps1 -WhatIf
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-admin.ps1 [-ActiveStart 23 -ActiveEnd 17] [-KeepAutoInstall]

  1. Windows Update may not reboot the machine while someone is signed in (it always is), automatic
     install off by default (-KeepAutoInstall leaves it on), and "active hours" pinned by policy to an
     18-hour window (23:00 -> 17:00 by default) so the night shift is covered too.
  2. Fast Startup and hibernation off: "shut down" becomes a real shutdown and every boot a clean one.
  3. No password on wake, no lock screen.
  4. The Task Scheduler history log on (so you can see what fired after a reboot).
  5. Auto sign-in: NOT done here - see the message at the end (Sysinternals Autologon, by hand, once).
#>
param([switch]$WhatIf, [int]$ActiveStart = 23, [int]$ActiveEnd = 17, [switch]$KeepAutoInstall, [switch]$Elevated)
$ErrorActionPreference = 'Continue'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIf) {
  $fwd = @('-Elevated', '-ActiveStart', $ActiveStart, '-ActiveEnd', $ActiveEnd)
  if ($KeepAutoInstall) { $fwd += '-KeepAutoInstall' }
  $argLine = '-NoProfile -ExecutionPolicy Bypass -NoExit -File "{0}" {1}' -f $PSCommandPath, ($fwd -join ' ')
  Write-Host 'Not elevated - asking for administrator rights (one UAC click) ...'
  Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argLine
  exit 0
}

function Reg($path, $name, $value, $type) {
  if ($WhatIf) { Write-Host "  would set $path\$name = $value"; return }
  if (-not (Test-Path -LiteralPath $path)) { New-Item -Path $path -Force | Out-Null }
  New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
  Write-Host "  set $path\$name = $value"
}
function Run($exe, [string[]]$a) {
  if ($WhatIf) { Write-Host "  would run $exe $($a -join ' ')"; return }
  $out = & $exe @a 2>&1 | Out-String
  Write-Host ("  {0} {1} -> exit {2} {3}" -f $exe, ($a -join ' '), $LASTEXITCODE, $out.Trim())
}

Write-Host '1. Windows Update'
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
Reg "$wu\AU" 'NoAutoRebootWithLoggedOnUsers' 1 'DWord'
Reg "$wu\AU" 'AlwaysAutoRebootAtScheduledTime' 0 'DWord'
if (-not $KeepAutoInstall) {
  Reg "$wu\AU" 'NoAutoUpdate' 1 'DWord'      # nothing installs by itself: install by hand, monthly
  Reg "$wu\AU" 'AUOptions' 2 'DWord'         # notify only
}
Reg $wu 'SetAutoRestartNotificationDisable' 1 'DWord'
Reg $wu 'SetActiveHours' 1 'DWord'
Reg $wu 'ActiveHoursStart' $ActiveStart 'DWord'
Reg $wu 'ActiveHoursEnd' $ActiveEnd 'DWord'

Write-Host '2. Fast Startup and hibernation off'
Run 'powercfg.exe' @('/hibernate', 'off')
Reg 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled' 0 'DWord'

Write-Host '3. No password on wake, no lock screen'
Run 'powercfg.exe' @('/setacvalueindex', 'SCHEME_CURRENT', 'SUB_NONE', 'CONSOLELOCK', '0')
Run 'powercfg.exe' @('/setactive', 'SCHEME_CURRENT')
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' 'NoLockScreen' 1 'DWord'

Write-Host '4. Task Scheduler history'
Run 'wevtutil.exe' @('sl', 'Microsoft-Windows-TaskScheduler/Operational', '/e:true')

Write-Host ''
Write-Host '5. LAST STEP, BY HAND: auto sign-in.' -ForegroundColor Cyan
Write-Host '   Download Sysinternals Autologon (Microsoft): https://learn.microsoft.com/sysinternals/downloads/autologon'
Write-Host '   Run it, type your Windows password once, click Enable. It stores the password as an encrypted'
Write-Host '   LSA secret - never as plain text in the registry. If you ever change the Windows password,'
Write-Host '   run Autologon again, or the machine silently boots to the sign-in screen.'
Write-Host ''
Write-Host 'Then: windows\always-on\check.ps1, and set the BIOS "After Power Loss" option to Power On.'
