<#
  check.ps1 -- READ-ONLY audit of the always-on recipe (windows\always-on\README.md). Changes nothing,
  needs no admin, never prints a password, a user name, a machine name or an address.

    powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\check.ps1

  [ok]   the setting is as the recipe wants it
  [WARN] it is not - the line says what to run
  [info] cannot be checked from here (the BIOS) or is only context (last boot, unclean shutdowns)
#>
$ErrorActionPreference = 'SilentlyContinue'
$script:warns = 0
function Ok($what, $detail) { Write-Host ("  [ok]   {0}{1}" -f $what, $(if ($detail) { " - $detail" } else { '' })) }
function Warn($what, $detail) { $script:warns++; Write-Host ("  [WARN] {0} - {1}" -f $what, $detail) }
function Info($what, $detail) { Write-Host ("  [info] {0} - {1}" -f $what, $detail) }
function RegVal($path, $name) { $i = Get-ItemProperty -LiteralPath $path -Name $name -ErrorAction SilentlyContinue; if ($i) { $i.$name } else { $null } }
function AcIndex($sub, $setting) {
  # /qh, not /query: some of these (CONSOLELOCK) are hidden settings that /query does not list.
  # The labels are translated on non-English Windows, so read by position instead: the LAST two lines that
  # end in a 0x value are the current AC index, then the current DC index (the min/max/step lines come first).
  $vals = @(powercfg /qh SCHEME_CURRENT $sub $setting 2>$null | Select-String '0x[0-9a-fA-F]{8}\s*$')
  if ($vals.Count -ge 2) { [Convert]::ToInt32((($vals[-2].Line -split ':')[-1]).Trim(), 16) } else { $null }
}

Write-Host "`nfirmware"
Info 'power on after power loss' "set once in the BIOS/UEFI setup (Windows cannot read it without admin or vendor tools): After Power Loss / Restore on AC Power Loss = Power On"

Write-Host "`nsign-in without a human"
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$auto = RegVal $wl 'AutoAdminLogon'
$names = @((Get-Item -LiteralPath $wl).GetValueNames())
if ("$auto" -eq '1') {
  if ($names -contains 'DefaultPassword') { Warn 'auto sign-in' 'ON, but the password sits in the registry as PLAIN TEXT (DefaultPassword) - use Sysinternals Autologon, which stores it as an encrypted LSA secret' }
  else { Ok 'auto sign-in' 'on; no plaintext password in the registry (an LSA secret, e.g. Sysinternals Autologon)' }
} else { Warn 'auto sign-in' 'off - after a reboot nothing per-user starts until someone signs in (README: Autologon)' }
if ("$(RegVal 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' 'NoLockScreen')" -eq '1') { Ok 'lock screen' 'off (policy NoLockScreen = 1)' }
else { Warn 'lock screen' 'on - harden-admin.ps1 sets NoLockScreen = 1' }
$cl = AcIndex 'SUB_NONE' 'CONSOLELOCK'
if ($cl -eq 0) { Ok 'password on wake' 'not required (CONSOLELOCK = 0)' } elseif ($null -eq $cl) { Info 'password on wake' 'not readable' } else { Warn 'password on wake' 'required - harden-admin.ps1 sets CONSOLELOCK 0' }
$ss = RegVal 'HKCU:\Control Panel\Desktop' 'ScreenSaverIsSecure'
if ("$ss" -ne '1') { Ok 'screensaver lock' 'off' } else { Warn 'screensaver lock' 'on - harden-user.ps1 turns it off' }

Write-Host "`nnever sleep"
$sb = AcIndex 'SUB_SLEEP' 'STANDBYIDLE'
if ($sb -eq 0) { Ok 'sleep on AC' 'never' } else { Warn 'sleep on AC' "after $([int]($sb / 60)) min - harden-user.ps1 sets it to never" }
$hb = AcIndex 'SUB_SLEEP' 'HIBERNATEIDLE'
if ($hb -eq 0 -or $null -eq $hb) { Ok 'hibernate on AC' 'never' } else { Warn 'hibernate on AC' "after $([int]($hb / 60)) min - harden-user.ps1" }
$fs = RegVal 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled'
if ("$fs" -eq '0') { Ok 'Fast Startup' 'off (a shutdown is a real one; boots are clean)' } else { Warn 'Fast Startup' 'on - harden-admin.ps1 turns it off' }
$vi = AcIndex 'SUB_VIDEO' 'VIDEOIDLE'
if ($null -ne $vi) { Info 'monitor timeout' $(if ($vi -eq 0) { 'never' } else { "$([int]($vi / 60)) min (harmless: only the screen sleeps)" }) }

Write-Host "`nWindows Update cannot reboot it mid-job"
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
if ("$(RegVal $au 'NoAutoRebootWithLoggedOnUsers')" -eq '1') { Ok 'no forced reboot while signed in' 'policy set' } else { Warn 'forced reboots' 'possible - harden-admin.ps1 sets NoAutoRebootWithLoggedOnUsers = 1' }
$nau = RegVal $au 'NoAutoUpdate'
if ("$nau" -eq '1') { Ok 'automatic install' 'off - install updates by hand, monthly (reboots are survivable now)' } else { Info 'automatic install' 'on - fine if active hours cover your working window' }
$s1 = RegVal 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' 'ActiveHoursStart'
$e1 = RegVal 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' 'ActiveHoursEnd'
if ($null -eq $s1) { $s1 = RegVal 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' 'ActiveHoursStart'; $e1 = RegVal 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' 'ActiveHoursEnd' }
if ($null -ne $s1) {
  $len = (([int]$e1 - [int]$s1) + 24) % 24
  if ($len -ge 16) { Ok 'active hours' ("{0:00}:00 -> {1:00}:00 ({2} h)" -f $s1, $e1, $len) } else { Warn 'active hours' ("{0:00}:00 -> {1:00}:00 is only {2} h - stretch it to 18 h" -f $s1, $e1, $len) }
}

Write-Host "`nthe tasks that bring it back"
foreach ($n in @('Jarvis-Dispatcher', 'Jarvis-Dispatcher-Restart', 'Jarvis-Dispatcher-Heal', 'Jarvis-Boot-Report')) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {
    $i = $t | Get-ScheduledTaskInfo
    Ok $n ("{0}; last run {1}; result {2}" -f $t.State, $i.LastRunTime, $i.LastTaskResult)
  } elseif ($n -eq 'Jarvis-Boot-Report') { Info $n 'not registered (optional: install-tasks.ps1 -BootReport)' }
  else { Warn $n 'not registered - windows\install-tasks.ps1' }
}

Write-Host "`nreachability"
$ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (Test-Path -LiteralPath $ts) {
  $pr = & $ts debug prefs 2>$null | ConvertFrom-Json
  if ($pr.ForceDaemon) { Ok 'Tailscale unattended mode' 'on - reachable even with nobody signed in' } else { Warn 'Tailscale unattended mode' 'off - turn on "Run unattended" so a remote logon is always possible' }
} else { Info 'remote access' 'no Tailscale found - any always-on remote access (VPN + RDP, SSH) is your safety net' }

Write-Host "`ncontext"
$os = Get-CimInstance Win32_OperatingSystem
Info 'last boot' ("{0:yyyy-MM-dd HH:mm} ({1:N1} h ago)" -f $os.LastBootUpTime, ((Get-Date) - $os.LastBootUpTime).TotalHours)
$dirty = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 41, 6008; StartTime = (Get-Date).AddDays(-30) } -ErrorAction SilentlyContinue).Count
Info 'unclean shutdowns, last 30 days' "$dirty (events 41/6008: power cuts, hard resets)"

Write-Host ("`n{0} warning(s)" -f $script:warns)
exit 0
