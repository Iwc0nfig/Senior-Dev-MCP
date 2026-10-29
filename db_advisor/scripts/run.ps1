# Serve the DB Advisor over the network (streamable-http), next to the Code Advisor.
#
# Host, port and path come from config.toml [server]; the parameters override them.
#
#   .\scripts\run.ps1                       as configured
#   .\scripts\run.ps1 -BindHost 127.0.0.1   local only
#
# NO authentication: trusted network only.  The server is read-only; its only live
# database access is validate_sql (compile-only, SET NOEXEC ON, read-only login).
[CmdletBinding()]
param([string]$BindHost, [int]$Port, [string]$Path)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }
Push-Location $root
try {
  function Get-Cfg([string]$key) {
    $v = & $py -m senior_dev_db.config get $key
    if ($LASTEXITCODE -ne 0) { throw "config.toml: cannot read $key" }
    return "$v".Trim()
  }
  if (-not $BindHost) { $BindHost = Get-Cfg "server.host" }
  if (-not $Port)     { $Port = [int](Get-Cfg "server.port") }
  if (-not $Path)     { $Path = Get-Cfg "server.path" }
  $db = Get-Cfg "paths.db_path"
  if (-not (Test-Path $db)) { throw "no index at $db. Run: .\scripts\prepare.ps1" }
  Write-Host "senior-dev-db -> http://${BindHost}:${Port}${Path}"
  if ($BindHost -eq "0.0.0.0") { Write-Host "listening on all interfaces with no authentication - trusted network only" -ForegroundColor Yellow }
  & $py -m senior_dev_db --transport streamable-http --host $BindHost --port $Port --path $Path
}
finally { Pop-Location }
exit $LASTEXITCODE
