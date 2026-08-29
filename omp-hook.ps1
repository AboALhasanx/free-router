# Client launch hook: ensures the free-router gateway is up before
# OpenCode / omp start. Wire this into both runtimes' launch so the
# unified gateway is always available.
#
# OpenCode:  call this from a small wrapper that starts opencode.
# omp:       call this from a small wrapper that starts omp.
# Simplest:  both `opencode` and `omp` launchers run free-router.bat first.
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
& "$here\free-router.bat"
