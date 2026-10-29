# Build the workspace copy before indexing it.
#
# Pipeline order (the scheduled job):
#
#   1. get latest  -> Workspace          (existing job)
#   2. build-workspace.ps1               (this script)
#   3. reindex.ps1 -RestartHosts         (graph + compiler index)
#
# Restores and builds every .csproj in the workspace with Visual Studio's
# MSBuild, refreshes each Website Project's Bin as a VS website build does, and
# asks ASP.NET for every page's generated code.  Writes only MSBuild output
# (bin/, obj/, website Bin) inside the workspace -- never a read-only
# (version-controlled) file.  Everything else, including the manifest.json the
# index pass reads, goes to .index/build/.
#
# A project that fails to build does not fail this script: the failure is in the
# manifest and the index reports the project as incomplete.  The script fails
# only if the step itself could not run (no MSBuild, no config, ...).
#
#   .\scripts\build-workspace.ps1
#   .\scripts\build-workspace.ps1 -Only Legacy/Legacy.Vault

[CmdletBinding()]
param(
  [string]$Only
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: python -m venv .venv; .\.venv\Scripts\pip install -e .[dev]" }

$argsList = @((Join-Path $root "scripts\build-workspace.py"))
if ($Only) { $argsList += @("--only", $Only) }

$sw = [Diagnostics.Stopwatch]::StartNew()
& $py @argsList
if ($LASTEXITCODE -ne 0) { throw "build step failed" }
$sw.Stop()
Write-Host "build step finished in $([int]$sw.Elapsed.TotalSeconds)s"
