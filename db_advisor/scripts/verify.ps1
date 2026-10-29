# Check that everything works ([ok] / [warn] / [FAIL]).  The URLs default to config.toml
# ([server] for this advisor, [code_advisor] url for the Code Advisor).
#
#   .\scripts\verify.ps1 -Stage prereq     before prepare: Python, packages, ODBC, DB login, workspace, parsers, Code Advisor
#   .\scripts\verify.ps1 -Stage index      after prepare: index complete, then the test suite
#   .\scripts\verify.ps1 -Stage server     with both servers running: MCP over HTTP + cross-check with the Code Advisor
#   .\scripts\verify.ps1                   all three
[CmdletBinding()]
param([ValidateSet("all","prereq","index","server")][string]$Stage = "all",
      [string]$DbUrl = "", [string]$CodeUrl = "")
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "[FAIL] venv not found at $py. Run: .\scripts\setup.ps1" -ForegroundColor Red; exit 1 }
$a = @((Join-Path $root "scripts\verify.py"), "--stage", $Stage)
if ($DbUrl) { $a += @("--db-url", $DbUrl) }
if ($CodeUrl) { $a += @("--code-url", $CodeUrl) }
Push-Location $root
try { & $py -X utf8 @a } finally { Pop-Location }
exit $LASTEXITCODE
