# The always-on PC — a machine that comes back by itself

Jarvis is only useful if the machine it runs on is *there* when you message it: after a Windows Update
reboot at 02:30, after a power cut, after someone presses the power button. This is the recipe the
original machine runs — a small office desktop, on 24/7 — and the scripts that apply and check it.

**Why it exists.** One night a Windows Update restarted the machine at 02:30. Nobody signed in, so
nothing that lives in a user session came back — the AI login, the bot poller, Docker, the cloud-drive
mount — and it sat there, "on" and useless, for two days. Everything below is the answer to that night.

> **Rule zero: a session is not a service.** The Claude login, the Telegram poller, Docker Desktop and any
> cloud drive are per-user, session-bound things. Running Jarvis "as SYSTEM" or as a service cannot see
> them. So the machine **signs itself in** and everything starts from logon tasks.

## The boot timeline it produces

Times are from Windows' own boot time (`LastBootUpTime`), measured on the real machine during unattended
restarts with nobody touching it, and re-confirmed on later reboots by the boot report. Before that, the
BIOS setting adds the power-cut path:

| after boot | what happens by itself |
|---|---|
| — | power returns → the BIOS powers the machine on (**After Power Loss = Power On**); Windows boots *clean*, because Fast Startup is off |
| ~0.5 min | **auto sign-in** creates the console session (no lock screen, no password prompt) |
| sign-in + 30 s | logon tasks: desktop apps |
| sign-in + 45 s | **Jarvis-Dispatcher**: the supervisor, then the bot poller, then the dispatcher session |
| sign-in + 60 s | CI runner loops (if you run self-hosted GitHub Actions runners) |
| sign-in + 3 min ≈ **3.2 min** | the **boot report** runs and finds everything already up: *"minutes since boot: 3.2, session 1"* — session 1 is the console, so no human (and no remote logon) was involved |

## 1. Firmware: power on after a power loss

Set once in the BIOS/UEFI setup (Windows cannot read it back without admin or vendor tools):
**After Power Loss → Power On** — HP calls it *After Power Loss*, others *Restore on AC Power Loss*,
*AC Back*, *State after G3*. Now a power cut, a tripped breaker or a pulled plug ends with the machine
booting by itself. Optional backstop: a daily BIOS RTC power-on time. A UPS is the only fix for the
filesystem risk of a hard power cut; software cannot help there.

## 2. Sign in without a human

- **Auto sign-in with [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon)**
  (Microsoft). It stores the password as an encrypted **LSA secret** — the registry then shows only
  `AutoAdminLogon = 1`, never a plaintext `DefaultPassword`. `check.ps1` warns if it finds the plaintext
  variant. **Caveat:** change your Windows password and the secret goes stale — the machine silently boots
  to the sign-in screen again. Re-run Autologon after every password change.
- **No lock screen** (`Personalization\NoLockScreen = 1`), **no password on wake**
  (`powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0`), screensaver lock off.

Yes, this trades physical-access security for availability: run it on a machine in a place you trust,
with disk encryption if it can walk away, and keep your remote access (step 6) locked down.

## 3. Never sleep

On mains power: `powercfg /change standby-timeout-ac 0` and `hibernate-timeout-ac 0` (the monitor may
still turn off — harmless). Hibernation and **Fast Startup off** (`powercfg /hibernate off`,
`HiberbootEnabled = 0`): with Fast Startup, "shut down" is really a hibernate, so drivers and stale state
survive it and the boot is not clean. Wake timers allowed, so a scheduled task can wake the machine.

## 4. Windows Update may not reboot it mid-job

- `NoAutoRebootWithLoggedOnUsers = 1` — never a forced reboot while someone is signed in (someone always is).
- **Active hours pinned by policy to 23:00 → 17:00** (`SetActiveHours = 1`, 18 hours, the maximum): the
  night shift is covered too, and the Settings app's "smart" active hours cannot drift it back.
- Automatic install off (`NoAutoUpdate = 1`, `AUOptions = 2` notify only). Install updates **by hand,
  about monthly** — cheap now that a reboot is survivable. (`harden-admin.ps1 -KeepAutoInstall` if you
  would rather keep automatic installs and rely on the reboot rules.)

## 5. The tasks that bring Jarvis back (`..\install-tasks.ps1`)

| Task | Trigger | What |
|---|---|---|
| Jarvis-Dispatcher | at logon + 45 s | the supervisor; the task instance lives exactly as long as it does (single instance, no time limit, restarted 3× on a failed launch) |
| Jarvis-Dispatcher-Restart | daily 06:45 | stop the poller + supervisor, start again → a fresh context every morning |
| Jarvis-Dispatcher-Heal | every 15 min at :05 :20 :35 :50 | start the dispatcher if its supervisor is gone; silent otherwise. Never on the quarter hours: it once fired in the same second as the 06:45 restart and raced it |
| Jarvis-Boot-Report | at logon + 3 min (optional) | `logs\boot-report.log`: minutes since boot, clean/unclean, which settings stuck |

All user-level ("run only when logged on" — deliberately, see rule zero), all started through
`run_hidden.vbs` so no console window ever flashes on the desktop. Inside the supervisor, the poller is
watched every 5 seconds (see the main README's *self-healing* section) — so the layers are: BIOS → auto
sign-in → logon task → 15-minute heal → supervisor guards → the daily fresh start.

## 6. Reachability: the safety net

Run your remote access in **unattended mode** (Tailscale: *Run unattended*; or a VPN + RDP/SSH that works
before sign-in). Then even if every layer above fails, you can still reach the machine — and a remote
logon itself creates the session that starts everything.

## 7. If you also host CI runners on it

The original machine also runs two self-hosted GitHub Actions runners. What kept them healthy:

- **Ephemeral runners** — each registration serves exactly one job, then re-registers with a fresh
  1-hour token minted on the host (no long-lived token inside the runner), and the work folder is wiped
  before and after every job.
- **An isolated WSL2 distro** with its own Docker engine, **no access to the Windows drives and no Windows
  interop** — CI code cannot touch the host.
- **One loop per slot, always:** each loop holds a named mutex (`Local\ci-runner-slot-N`). The scheduled
  task fires at logon **and every 15 minutes**; a second instance just waits on the mutex, so a repeat
  trigger can never double a loop, and a dead loop is replaced within 15 minutes. ("Restart on failure"
  in Task Scheduler only covers launch failures — it would not have caught a loop that was killed.)
- **Recycle one slot at a time, and only when it is idle** — never both, and never `wsl --shutdown` while a
  job runs.
- **Clean up after every job**: dangling volumes (a Postgres service container leaves ~500 MB behind each
  run), stopped containers, unused networks.

## The scripts

```powershell
# read-only: what is set, what is not (no admin, prints no names, passwords or addresses)
powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\check.ps1

# apply - every script has -WhatIf to show the plan first
powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-user.ps1 -WhatIf
powershell -NoProfile -ExecutionPolicy Bypass -File windows\always-on\harden-admin.ps1 -WhatIf   # self-elevates without -WhatIf
powershell -NoProfile -ExecutionPolicy Bypass -File windows\install-tasks.ps1 -BootReport -WhatIf
```

Then the real test: **pull the plug.** Plug it back in, walk away, and read `logs\boot-report.log` a few
minutes later. A report written ~3 minutes after boot, in session 1, can only have come from the machine
doing everything by itself.
