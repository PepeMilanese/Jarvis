# dispatcher.ps1 -- what the Jarvis-Dispatcher scheduled task runs: the supervisor, in the FOREGROUND,
# so the task instance lives exactly as long as the dispatcher does.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File windows\dispatcher.ps1
#
# The Python that runs Jarvis: $env:JARVIS_PYTHON if set, else the `py` launcher, else `python`.
# The supervisor logs to logs\dispatcher.log itself; its raw stdout/stderr (a crash traceback, say) goes
# to logs\dispatcher-super.out.txt.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

# a plain session: nothing inherited from a Claude Code session that may have started this by hand
Get-ChildItem Env: | Where-Object { $_.Name -like 'CLAUDE_*' -or $_.Name -eq 'CLAUDECODE' } |
  ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" -ErrorAction SilentlyContinue }
# the Telegram plugin's server runs on Bun
$bun = Join-Path $HOME '.bun\bin'
if ((Test-Path -LiteralPath (Join-Path $bun 'bun.exe')) -and ($env:PATH -notlike "*$bun*")) { $env:PATH = "$bun;$env:PATH" }

$py = $env:JARVIS_PYTHON
if (-not $py) {
  $c = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $c) { $c = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 }
  if (-not $c) { Write-Error 'no Python found (set JARVIS_PYTHON)'; exit 1 }
  $py = $c.Source
}
$state = if ($env:JARVIS_STATE) { $env:JARVIS_STATE } else { $root }   # as in Python: runtime files live here
$logs = Join-Path $state 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$out = Join-Path $logs 'dispatcher-super.out.txt'
# cmd.exe does the redirection: bytes pass through as-is (PowerShell 5.1's *> would write UTF-16)
$line = '"{0}" -m jarvis dispatcher >> "{1}" 2>&1' -f $py, $out
& cmd.exe /d /s /c "`"$line`""
exit $LASTEXITCODE
