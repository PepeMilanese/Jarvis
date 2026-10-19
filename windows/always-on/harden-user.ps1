<#
  harden-user.ps1 -- the always-on settings that need NO administrator rights. Idempotent.

    powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-user.ps1 -WhatIf   # show only
    powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-user.ps1           # apply

  - the screensaver off, and never a password after it
  - on mains power: never sleep, never hibernate (the screen may still turn off: -MonitorMinutes)
  - wake timers allowed, so a scheduled task may wake the machine
  Then run check.ps1 to see the result.
#>
param([switch]$WhatIf, [int]$MonitorMinutes = 0)
$ErrorActionPreference = 'Continue'

function Reg($path, $name, $value, $type) {
  if ($WhatIf) { Write-Host "  would set $path\$name = $value"; return }
  if (-not (Test-Path -LiteralPath $path)) { New-Item -Path $path -Force | Out-Null }
  New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
  Write-Host "  set $path\$name = $value"
}
function Power([string[]]$a) {
  if ($WhatIf) { Write-Host "  would run powercfg $($a -join ' ')"; return }
  $out = & powercfg.exe @a 2>&1 | Out-String
  Write-Host ("  powercfg {0} -> exit {1} {2}" -f ($a -join ' '), $LASTEXITCODE, $out.Trim())
}

Write-Host 'screensaver'
Reg 'HKCU:\Control Panel\Desktop' 'ScreenSaveActive' '0' 'String'
Reg 'HKCU:\Control Panel\Desktop' 'ScreenSaverIsSecure' '0' 'String'
Reg 'HKCU:\Control Panel\Desktop' 'ScreenSaveTimeOut' '0' 'String'

Write-Host 'power (mains)'
Power @('/change', 'standby-timeout-ac', '0')
Power @('/change', 'hibernate-timeout-ac', '0')
Power @('/change', 'monitor-timeout-ac', "$MonitorMinutes")
Power @('/setacvalueindex', 'SCHEME_CURRENT', 'SUB_SLEEP', 'RTCWAKE', '1')
Power @('/setactive', 'SCHEME_CURRENT')
Write-Host 'done - now run check.ps1 (and harden-admin.ps1 for the rest)'
