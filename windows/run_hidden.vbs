' run_hidden.vbs -- windowless launcher for the Jarvis scheduled tasks.
'
' Task Scheduler runs THIS (wscript.exe), not powershell.exe, so no console window ever flashes on the
' desktop: `powershell -WindowStyle Hidden` still shows a window for a moment when the scheduler starts it
' under an interactive token, and `conhost --headless` loses the exit code. wscript + Run(cmd, 0, True)
' does neither.
'
' Usage (the task's Arguments field - quote both paths, they may contain spaces):
'   wscript.exe //B //Nologo "<jarvis>\windows\run_hidden.vbs" "<jarvis>\windows\<script>.ps1" [args...]
'
' It WAITS for the script. That matters for the dispatcher task: the task instance must live exactly as
' long as the supervisor, so "MultipleInstances = IgnoreNew" guarantees one dispatcher (two pollers on
' one bot token split the messages) and Stop-ScheduledTask ends the whole tree.

Option Explicit

Dim fso, sh, target, cmd, rc, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then WScript.Quit 3
target = WScript.Arguments(0)
If Not fso.FileExists(target) Then WScript.Quit 2

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & target & Chr(34)
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & Chr(34) & WScript.Arguments(i) & Chr(34)
Next

rc = sh.Run(cmd, 0, True)
WScript.Quit rc
