# DB Advisor pipeline: setup -> check prerequisites -> prepare index + sandbox -> check index.
#
# Every step is one script in scripts\, called as it is: a change to a script is a
# change to this pipeline and to ..\full_pipeline.ps1.  Each script also runs on its
# own.  Stops at the first step that fails.  Serving is separate: scripts\run.ps1.
#
#   .\db_advisor_pipeline.ps1
#   .\db_advisor_pipeline.ps1 -SkipSetup   # .venv and parsers already installed: skip scripts\setup.ps1
#
# Needs .env (read-only SQL login, see .env.example) and the Code Advisor's index
# (the code_members phase reads its graph.db), so run the Code Advisor pipeline first.
# Stop the DB Advisor server first: Windows will not replace an open index file.
[CmdletBinding()]
param([switch]$SkipSetup)
$ErrorActionPreference = "Stop"
$scripts = Join-Path $PSScriptRoot "scripts"

function Invoke-Step([string]$name, [scriptblock]$run) {
  Write-Host ""
  Write-Host "==> [db_advisor] $name" -ForegroundColor Cyan
  $global:LASTEXITCODE = 0
  & $run
  if ($LASTEXITCODE -ne 0) { throw "[db_advisor] $name failed (exit $LASTEXITCODE)" }
}

$sw = [Diagnostics.Stopwatch]::StartNew()
if (-not $SkipSetup) { Invoke-Step "setup" { & (Join-Path $scripts "setup.ps1") } }
Invoke-Step "check prerequisites" { & (Join-Path $scripts "verify.ps1") -Stage prereq }
Invoke-Step "prepare + sandbox"   { & (Join-Path $scripts "prepare.ps1") -Sandbox }
Invoke-Step "check index"         { & (Join-Path $scripts "verify.ps1") -Stage index }
$sw.Stop()
Write-Host ""
Write-Host "db_advisor pipeline finished in $([int]$sw.Elapsed.TotalMinutes) min. Serve: .\scripts\run.ps1" -ForegroundColor Green
