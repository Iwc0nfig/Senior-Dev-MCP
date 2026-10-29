# One-time setup on a machine (the VM next to the Code Advisor, or a dev box).
#
#   .\scripts\setup.ps1                  # venv + packages + parsers
#   .\scripts\setup.ps1 -Python "C:\Python313\python.exe"
#
# Packages come from .\wheelhouse when it exists (no internet needed), otherwise
# from PyPI.  The two C# parsers ship prebuilt in sql-parser\bin and
# code-extractor\bin; with a .NET SDK present they are rebuilt from source.
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
  Write-Host "==> installing packages"
  $done = $false
  if (Test-Path "wheelhouse") {
    & $py -m pip install --no-index --find-links wheelhouse -r requirements.txt
    $done = ($LASTEXITCODE -eq 0)
    if (-not $done) { Write-Host "offline install incomplete for this Python; trying PyPI" -ForegroundColor Yellow }
  }
  if (-not $done) { & $py -m pip install --find-links wheelhouse -r requirements.txt }
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
  # put src\ on the venv's path (no build backend needed)
  $site = & $py -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
  Set-Content -Path (Join-Path $site "senior_dev_db.pth") -Value (Join-Path $root "src") -Encoding ascii

  if (Get-Command dotnet -ErrorAction SilentlyContinue) {
    $sdks = & dotnet --list-sdks 2>$null
    if ($sdks) {
      Write-Host "==> building the C# parsers"
      & dotnet build sql-parser -c Release --nologo -v q
      if ($LASTEXITCODE -ne 0) { throw "sql-parser build failed" }
      & dotnet build code-extractor -c Release --nologo -v q
      if ($LASTEXITCODE -ne 0) { throw "code-extractor build failed" }
    } else { Write-Host "no .NET SDK: using the prebuilt parsers" }
  }
  if (-not (Test-Path ".env")) {
    Write-Host "NOTE: create .env (see .env.example) with the read-only SQL login before prepare." -ForegroundColor Yellow
  }
  Write-Host "setup done. Next: .\scripts\verify.ps1 -Stage prereq"
} finally { Pop-Location }
