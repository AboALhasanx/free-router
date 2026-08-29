# Launcher / on-demand starter for the free-router gateway.
# Starts the router only if it is not already listening.
# Port is dynamic: reads FREE_ROUTER_PORT env, then config.yml, then 8765.
# If the preferred port is busy, router auto-picks the next free one and writes port.txt.
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$cfgPort = 8765
try {
    $yml = Get-Content (Join-Path $here "config.yml") -Raw -ErrorAction SilentlyContinue
    if ($yml -match '^\s*port:\s*(\d+)') { $cfgPort = [int]$Matches[1] }
} catch {}
$port = if ($env:FREE_ROUTER_PORT) { [int]$env:FREE_ROUTER_PORT } else { $cfgPort }
$bind = if ($env:FREE_ROUTER_BIND) { $env:FREE_ROUTER_BIND } else { "127.0.0.1" }

# check preferred port first; if busy, also check port.txt for the actual chosen port
$checkPorts = @($port)
$portFile = Join-Path $here "port.txt"
if (Test-Path $portFile) {
    try { $actual = [int](Get-Content $portFile -Raw).Trim(); if ($actual -ne $port) { $checkPorts += $actual } } catch {}
}
$listening = $false
$livePort = $null
foreach ($p in $checkPorts) {
    try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect($bind, $p); $listening=$true; $livePort=$p; $c.Close(); break } catch {}
}
if ($listening) { Write-Host "free-router already listening on ${bind}:$livePort"; exit 0 }

$py = (Get-Command python -ErrorAction Stop).Source
$router = Join-Path $here "router.py"
Write-Host "Starting free-router (preferred ${bind}:$port) ..."
Start-Process -FilePath $py -ArgumentList "`"$router`"" -WorkingDirectory $here -WindowStyle Hidden
Start-Sleep -Seconds 4
# discover actual port (router writes port.txt)
$actualPort = $port
if (Test-Path $portFile) { try { $actualPort = [int](Get-Content $portFile -Raw).Trim() } catch {} }
try {
    $c = New-Object System.Net.Sockets.TcpClient; $c.Connect($bind, $actualPort); $c.Close()
    Write-Host "free-router is up on http://${bind}:$actualPort/v1  (dashboard http://${bind}:$actualPort/)"
} catch {
    # try next ports if preferred was busy
    $found=$false
    for ($p=$port+1; $p -lt $port+20; $p++) {
        try { $c2=New-Object System.Net.Sockets.TcpClient; $c2.Connect($bind,$p); $c2.Close(); Write-Host "free-router is up on http://${bind}:$p/v1"; $found=$true; break } catch {}
    }
    if (-not $found) { Write-Host "free-router did NOT come up. Check $here\log\stderr.log and $here\log\router.log"; exit 1 }
}
