# Serve the advisor over the network instead of stdio.
#
# One long-lived process on the LAN, rather than one subprocess per client: the
# two Roslyn solutions take 3.3 s and 9.1 s to load, which a stdio client pays
# every session; here it is paid once.
#
# Host, port and path come from config.toml [server]; the parameters override them.
#
#   .\scripts\run.ps1                       as configured
#   .\scripts\run.ps1 -Port 9000            another port
#   .\scripts\run.ps1 -BindHost 127.0.0.1   local only
#   .\scripts\run.ps1 -NoRoslyn             graph + filesystem layers only
#
# There is NO authentication and the MCP SDK leaves DNS-rebinding protection off
# unless TransportSecuritySettings is passed, so 0.0.0.0 belongs on a trusted
# network only.  The server is read-only by construction (no tool writes, edits
# or executes), but the codebase it exposes is still yours.

[CmdletBinding()]
param(
  [string]$BindHost,
  [int]$Port,
  [string]$Path,
  [switch]$NoRoslyn,
  [switch]$Stateless,
  [string]$Config
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }

$cfgArgs = @(); if ($Config) { $cfgArgs = @("--config", $Config) }
function Get-Cfg([string]$key) {
  $v = & $py -m senior_dev_code.config get $key @cfgArgs
  if ($LASTEXITCODE -ne 0) { throw "config.toml: cannot read $key" }
  return "$v".Trim()
}
if (-not $BindHost) { $BindHost = Get-Cfg "server.host" }
if (-not $Port)     { $Port = [int](Get-Cfg "server.port") }
if (-not $Path)     { $Path = Get-Cfg "server.path" }

# An empty index answers "no_results" to everything, indistinguishable from a
# real absence.  Fail loudly instead.
$db = Get-Cfg "paths.db_path"
if (-not (Test-Path $db)) { throw "no index at $db. Run: .\scripts\reindex.ps1" }

$argsList = @("-m", "senior_dev_code", "--transport", "streamable-http",
              "--host", $BindHost, "--port", $Port, "--path", $Path)
if ($NoRoslyn)  { $argsList += "--no-roslyn" }
if ($Stateless) { $argsList += "--stateless-http" }
$argsList += $cfgArgs

Write-Host "senior-dev-code -> http://${BindHost}:${Port}${Path}"
if ($BindHost -eq "0.0.0.0") {
  Write-Host "listening on all interfaces with no authentication - trusted network only" -ForegroundColor Yellow
}
if (-not $NoRoslyn) {
  Write-Host "Roslyn solutions load in the background; until then those tools answer 'unavailable'."
}

& $py @argsList
exit $LASTEXITCODE
