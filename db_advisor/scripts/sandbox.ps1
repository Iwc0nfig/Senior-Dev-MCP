# Build (or rebuild) the sandbox clone that test_sql runs scripts in.
#
#   .\scripts\sandbox.ps1
#
# Creates [sandbox] database (config.toml) on [sandbox] server: appdb' schema, ~10 rows
# per table (small reference tables whole, plus the parent rows they reference), every
# view / function / procedure / trigger, and a snapshot used to reset it.  Takes under a
# minute.  Safe to re-run at any time: it only ever drops a database it built itself.
# Failures (objects broken in appdb too) are listed in .index\sandbox_report.json.
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }
Push-Location $root
try {
  & $py -m senior_dev_db.prep.sandbox
  if ($LASTEXITCODE -ne 0) { throw "sandbox build failed" }
} finally { Pop-Location }
