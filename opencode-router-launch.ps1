# OpenCode launch wrapper: start the gateway (if needed) then run opencode.
# Replace your opencode launcher shortcut / alias with a call to this script,
# e.g.  powershell -ExecutionPolicy Bypass -File opencode-router-launch.ps1
$here = "C:\Users\gokoq\.free-router"
& "$here\free-router.bat" | Out-Null
& "C:\Users\gokoq\.bun\bin\opencode.exe" @args
