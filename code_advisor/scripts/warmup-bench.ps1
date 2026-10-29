# The load benchmark, and the go/no-go gate for deployment shape.
#
# Measures a cold MSBuildWorkspace load of each solution: elapsed time, peak
# working set, projects loaded, and the WorkspaceFailed list.
#
# Acceptance thresholds:
#   < 5 min, < 6 GB   -> run both hosts as long-lived processes with the MCP server
#   5-15 min          -> daemonise the hosts so load is paid once per boot
#   > 15 min or OOM   -> split the solution into project groups
#
# Measured on this machine 2026-09-04:
#   Legacy.sln       8/8 projects + App.Website (815 docs)   8.3 s   466 MB
#   Modern.sln  34/40 projects                         27.4 s   549 MB
# Both are comfortably in the first band.

[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv not found at $py. Run: .\scripts\setup.ps1" }

# Every path comes from config.toml.
function Get-CfgPath([string]$key) {
  $v = "$(& $py -m senior_dev_code.config get "paths.$key")".Trim()
  if ($LASTEXITCODE -ne 0 -or -not $v -or $v -eq "None") { throw "config.toml: missing [paths] $key" }
  return $v
}

$net48Exe = Get-CfgPath "roslyn_host_net48"
$net8Exe  = Get-CfgPath "roslyn_host_net8"
foreach ($e in @($net48Exe, $net8Exe)) {
  if (-not (Test-Path $e)) { throw "$e not built. Run scripts\build-hosts.ps1 first." }
}

$net48Sln = Get-CfgPath "net48_solution"
$net8Sln  = Get-CfgPath "net8_solution"
$website  = Get-CfgPath "website_root"
$refs     = Get-CfgPath "netfx_ref_assemblies"
$designer = Get-CfgPath "designer_dir"

# The hosts log to stderr by design (stdout is the RPC channel), and under
# $ErrorActionPreference = 'Stop' PowerShell turns any native stderr line into a
# terminating NativeCommandError, so it is relaxed here only.
$ErrorActionPreference = "Continue"

function Invoke-Bench {
  param([string]$Exe, [string[]]$BenchArgs)
  & $Exe @BenchArgs 2>&1 |
    ForEach-Object { "$_" } |
    Where-Object { $_ -notmatch '^\[bench\] \d+/' }
}

Write-Host "=========== net48: $net48Sln ==========="
Invoke-Bench $net48Exe @("--bench", $net48Sln, "--side", "net48",
                         "--website", $website, "--framework-refs", $refs,
                         "--designer-dir", $designer)

Write-Host ""
Write-Host "=========== net8: $net8Sln ==========="
Invoke-Bench $net8Exe @("--bench", $net8Sln, "--side", "net8")
