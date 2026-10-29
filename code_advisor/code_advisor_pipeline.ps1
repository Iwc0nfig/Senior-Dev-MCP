# Code Advisor pipeline: setup -> build hosts -> build workspace -> ASP.NET controls -> reindex -> tests.
#
# Every step is one script in scripts\, called as it is: a change to a script is a
# change to this pipeline and to ..\full_pipeline.ps1.  Each script also runs on its
# own.  Stops at the first step that fails.  Serving is separate: scripts\run.ps1.
#
#   .\code_advisor_pipeline.ps1
#   .\code_advisor_pipeline.ps1 -SkipSetup   # .venv already installed: skip scripts\setup.ps1
#
# Stop the server and the Roslyn hosts first: Windows will not replace an open graph.db.
[CmdletBinding()]
param([switch]$SkipSetup)
$ErrorActionPreference = "Stop"
$scripts = Join-Path $PSScriptRoot "scripts"

function Invoke-Step([string]$name, [scriptblock]$run) {
  Write-Host ""
  Write-Host "==> [code_advisor] $name" -ForegroundColor Cyan
  $global:LASTEXITCODE = 0
  & $run
  if ($LASTEXITCODE -ne 0) { throw "[code_advisor] $name failed (exit $LASTEXITCODE)" }
}

$sw = [Diagnostics.Stopwatch]::StartNew()
if (-not $SkipSetup) { Invoke-Step "setup" { & (Join-Path $scripts "setup.ps1") } }
Invoke-Step "build hosts"      { & (Join-Path $scripts "build-hosts.ps1") }
Invoke-Step "build workspace"  { & (Join-Path $scripts "build-workspace.ps1") }
# reflects over .NET Framework assemblies: needs Windows PowerShell 5.1, not pwsh
Invoke-Step "ASP.NET controls" { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scripts "gen_aspnet_controls.ps1") }
Invoke-Step "reindex"          { & (Join-Path $scripts "reindex.ps1") }
Invoke-Step "tests"            { & (Join-Path $scripts "test.ps1") }
$sw.Stop()
Write-Host ""
Write-Host "code_advisor pipeline finished in $([int]$sw.Elapsed.TotalMinutes) min. Serve: .\scripts\run.ps1" -ForegroundColor Green
