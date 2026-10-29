# Rebuild the index.
#
# Every run is a full rebuild: graph.db and its -wal/-shm sidecars are deleted
# first, then rebuilt from the workspace (~6 minutes).  Everything else under
# .index/ (aspnet_controls.json, designer/, logs/) is left in place.
#
# Stop the MCP server and the Roslyn hosts first -- Windows will not delete a
# graph.db another process holds open, and the pass stops with an error.
#
#   .\scripts\reindex.ps1
#   .\scripts\reindex.ps1 -RestartHosts

[CmdletBinding()]
param(
  [switch]$RestartHosts
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: python -m venv .venv; .\.venv\Scripts\pip install -e .[dev]" }

$argsList = @((Join-Path $root "scripts\reindex.py"))

$sw = [Diagnostics.Stopwatch]::StartNew()
& $py @argsList
if ($LASTEXITCODE -ne 0) { throw "index pass failed" }
$sw.Stop()
Write-Host "index pass finished in $([int]$sw.Elapsed.TotalSeconds)s"

# A .csproj or .sln change makes the Roslyn workspace stale.  Do NOT hot-reload
# it: a half-reloaded workspace gives confidently wrong references.  Restart the
# hosts instead.
if ($RestartHosts) {
  Write-Host "restarting Roslyn hosts..."
  Get-Process RoslynHost -ErrorAction SilentlyContinue | Stop-Process -Force
  & (Join-Path $PSScriptRoot "start-hosts.ps1")
}
