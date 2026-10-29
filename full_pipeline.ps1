# Both advisors, end to end: code_advisor\code_advisor_pipeline.ps1, then
# db_advisor\db_advisor_pipeline.ps1 (the DB Advisor reads the Code Advisor's index).
#
# This file only calls the two advisor pipelines, which only call their scripts\:
# a change to any of them applies here at once.  Stops at the first failure.
# Serving is separate: <advisor>\scripts\run.ps1 for each.
#
#   .\full_pipeline.ps1
#   .\full_pipeline.ps1 -SkipSetup   # routine rebuild: both setup steps skipped
[CmdletBinding()]
param([switch]$SkipSetup)
$ErrorActionPreference = "Stop"
$sw = [Diagnostics.Stopwatch]::StartNew()
& (Join-Path $PSScriptRoot "code_advisor\code_advisor_pipeline.ps1") -SkipSetup:$SkipSetup
& (Join-Path $PSScriptRoot "db_advisor\db_advisor_pipeline.ps1") -SkipSetup:$SkipSetup
$sw.Stop()
Write-Host ""
Write-Host "full pipeline finished in $([int]$sw.Elapsed.TotalMinutes) min." -ForegroundColor Green
Write-Host "Serve:  code_advisor\scripts\run.ps1   and   db_advisor\scripts\run.ps1"
