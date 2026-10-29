# One-time setup on a machine: the venv and the package (editable).
#
#   .\scripts\setup.ps1
#   .\scripts\setup.ps1 -Python "C:\Python313\python.exe"
#
# Packages come from .\wheelhouse when it exists (mcp.zip ships one: no internet
# needed), otherwise from PyPI.  The C# hosts are built by build-hosts.ps1.
[CmdletBinding()]
param([string]$Python = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  $pyArgs = @()
  if (-not $Python) {
    # prefer the versions the bundled wheelhouse covers (3.13, 3.12, 3.14)
    $Python = "python"
    if (Get-Command py -ErrorAction SilentlyContinue) {
      foreach ($v in @("3.13", "3.12", "3.14")) {
        & py "-$v" -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = "py"; $pyArgs = @("-$v"); break }
      }
    }
  }
  if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "==> creating .venv"
    & $Python @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "could not create the venv with '$Python'" }
  }
  $py = Join-Path $root ".venv\Scripts\python.exe"
  Write-Host "==> installing the package"
  $done = $false
  if (Test-Path "wheelhouse") {
    & $py -m pip install --no-index --find-links wheelhouse -e ".[dev]"
    $done = ($LASTEXITCODE -eq 0)
    if (-not $done) { Write-Host "offline install incomplete for this Python; trying PyPI" -ForegroundColor Yellow }
  }
  if (-not $done) { & $py -m pip install --find-links wheelhouse -e ".[dev]" }
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
  Write-Host "setup done. Next: .\scripts\build-hosts.ps1"
} finally { Pop-Location }
