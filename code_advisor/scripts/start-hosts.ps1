# Verify the Roslyn hosts standalone: -Verify loads both solutions once and prints
# their state.  The MCP server starts and owns the hosts itself (the load benchmark
# measured both loads inside the "good" band on this machine: 8.3 s and 27.4 s).

[CmdletBinding()]
param(
  [string]$Configuration = "Release",
  [switch]$Verify
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if ($Verify) {
  & $py -c @"
import time
from senior_dev_code.config import load_config
from senior_dev_code.roslyn.client import RoslynPool
cfg = load_config()
pool = RoslynPool(cfg)
t0 = time.time()
for side, st in pool.start_all(load=True).items():
    print(f'{side:6} state={st.state:8} projects={st.projects_loaded}/{st.projects_total} '
          f'website_docs={st.website_documents} failures={st.failure_count} err={st.error}')
print(f'total warm-up: {time.time() - t0:.1f}s')
pool.stop_all()
"@
  exit $LASTEXITCODE
}

Write-Host "The hosts are started and owned by the MCP server process."
Write-Host "Run with -Verify to load both solutions once and print their state."
