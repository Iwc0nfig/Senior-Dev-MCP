# Run the test suite against the real index and workspace.
#
#   .\scripts\test.ps1           graph + filesystem layers (live-host tests skip)
#   .\scripts\test.ps1 -Live     also the compiler-layer tests against live Roslyn hosts
[CmdletBinding()]
param([switch]$Live)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }
if ($Live) { $env:SENIOR_DEV_ROSLYN = "1" }
Push-Location $root
try { & $py -m pytest tests -q -p no:cacheprovider }
finally { Pop-Location; if ($Live) { Remove-Item Env:SENIOR_DEV_ROSLYN } }
exit $LASTEXITCODE
