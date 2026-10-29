# Rebuild the DB Advisor index (.index\dbadvisor.db) from the database and the workspace.
#
# Stop the DB Advisor server first: Windows will not replace an index file that the
# server holds open.  ~10-15 minutes (profiling reads every table, read-only, NOLOCK).
#
#   .\scripts\prepare.ps1
#   .\scripts\prepare.ps1 -Phases link,style,search     # re-run some phases only
#   .\scripts\prepare.ps1 -Sandbox                      # also rebuild the sandbox clone (test_sql)
#
# Curated facts (knowledge\curated, produced by Haiku in a Claude Code session) are
# re-verified against THIS machine's workspace before they are loaded: a fact whose
# cited line no longer contains it is dropped.
[CmdletBinding()]
param([string]$Phases = "", [switch]$Sandbox)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }
$sw = [Diagnostics.Stopwatch]::StartNew()
Push-Location $root
try {
  if ($Phases) {
    & $py -m senior_dev_db.prep --phases $Phases --finalize
    if ($LASTEXITCODE -ne 0) { throw "prepare failed" }
  } else {
    & $py -m senior_dev_db.prep --phases catalog,profile,lookups,sqlparse,code
    if ($LASTEXITCODE -ne 0) { throw "prepare failed (extraction phases)" }
    & $py -m senior_dev_db.prep.verify
    if ($LASTEXITCODE -ne 0) { throw "curated fact verification failed" }
    & $py -m senior_dev_db.prep --phases curated_load,link,code_members,style,search --finalize
    if ($LASTEXITCODE -ne 0) { throw "prepare failed (link phases)" }
  }
  if ($Sandbox) {
    & $py -m senior_dev_db.prep.sandbox
    if ($LASTEXITCODE -ne 0) { throw "sandbox build failed" }
  }
} finally { Pop-Location }
$sw.Stop()
Write-Host "prepare finished in $([int]$sw.Elapsed.TotalSeconds)s. Next: .\scripts\verify.ps1 -Stage index"
