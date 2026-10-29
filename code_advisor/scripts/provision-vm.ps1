# Provision a Windows VM to build the graph and host the advisor.
#
# What this machine has to become
# -------------------------------
# A Windows host runs the whole pipeline -- build step, indexer, both Roslyn
# hosts, every tool -- exactly like the dev box.
#
# The pipeline it hosts (the scheduled job):
#
#   get latest -> Workspace -> code_advisor_pipeline.ps1 (or ..\full_pipeline.ps1) -> run.ps1
#
# build-workspace.ps1 restores and builds all ~340 projects with VS MSBuild and asks
# ASP.NET for every page's generated code; reindex.ps1 then builds the graph and the
# compiler index.  Measured on the dev box: build ~5 min, reindex ~12 min, peak
# 3.7 GB in the net48 compiler pass, .index/ ~4 GB, NuGet cache up to ~17 GB.
#
# So the target is "a clone of the dev box", and the versions below are the dev
# box's, not the newest available.  Four of them are load-bearing:
#
#   Visual Studio 17.14   RoslynHost.csproj pins MSBuildPkgVersion 17.14.8 because
#                         the net48 host loads MSBuild IN-PROCESS via MSBuildLocator
#                         (Roslyn 4.8.0 predates the BuildHost).  In-process, the
#                         compile-time contract must match the assemblies on disk:
#                         with packages at 17.13.9 every legacy project fails with
#                         "Method not found: System.ReadOnlySpan<Char>".
#
#   .NET SDK 9            <RollForward>LatestMajor</RollForward> exists because the
#                         dev box has SDK 9 only and a net8.0 process on the 8.0
#                         runtime filters SDK 9 out as incompatible.
#
#   .NET Framework        gen_aspnet_controls.ps1 reflects over System.Web.dll and
#   4.7.2 targeting pack  System.Web.Extensions.dll in the 4.7.2 reference assemblies.
#                         Without it there is no control table, and markup type
#                         resolution falls back on 9,173 tags instead of 0.
#
#   Reference assemblies  86 projects target .NET Framework 4.5 - 4.7.1, which no
#   4.5 - 4.8.1           Visual Studio component installs.  Step 7 assembles one
#                         root holding every version (signed
#                         Microsoft.NETFramework.ReferenceAssemblies packages plus
#                         the installed 4.7.2/4.8/4.8.1); config.toml [build]
#                         target_framework_root defaults to it (build-workspace
#                         also fills it).  Without it those projects compile
#                         against nothing.
#
# This script is idempotent and safe to re-run.  It installs nothing until the
# egress preflight passes, and it reports state rather than assuming it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\provision-vm.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\provision-vm.ps1 -VerifyOnly
#
# Visual Studio Build Tools IS installed here (unless -VerifyOnly or
# -SkipVsInstall).  17.14 is the final VS 2022 minor and stays on the Current
# channel, so the plain bootstrapper can only land on 17.14.x.  Patch drift
# inside 17.14 is checked below and proven by start-hosts.ps1 -Verify.
#
# An existing Build Tools install that lacks a required component is MODIFIED in
# place.  A full IDE install is never modified automatically: its workload IDs
# differ (ManagedDesktop / NetWeb), and changing a developer's IDE unasked is
# not this script's call.

[CmdletBinding()]
param(
  [string]$WorkspacesRoot = "YourWorkspacePath",
  # The code_advisor folder (holds config.toml); default: the one this script is in.
  [string]$AdvisorRoot    = (Split-Path -Parent $PSScriptRoot),
  [string]$McpSubnet      = "10.0.0.0/8",
  # Default: config.toml [server] port.  Python is not installed yet, so read the line itself.
  [int]   $McpPort        = 0,
  [switch]$VerifyOnly,
  [switch]$SkipEgressCheck,
  [switch]$SkipVsInstall,
  # NuGet's global packages folder OF THE ACCOUNT THAT RUNS THE PIPELINE.  Run this
  # script as that account, or pass its path.
  [string]$NuGetCache     = (Join-Path $env:USERPROFILE ".nuget\packages")
)

$ErrorActionPreference = "Stop"
if (-not $McpPort) {
  $cfgToml = Join-Path $AdvisorRoot "config.toml"
  $section = ""
  foreach ($line in Get-Content $cfgToml) {
    if ($line -match '^\s*\[([^\]]+)\]') { $section = $Matches[1].Trim() }
    elseif ($section -eq "server" -and $line -match '^\s*port\s*=\s*(\d+)') { $McpPort = [int]$Matches[1]; break }
  }
  if (-not $McpPort) { throw "no [server] port in $cfgToml; pass -McpPort" }
}
# Windows PowerShell 5.1 renders Invoke-WebRequest progress so slowly that a
# 5 MB download can take minutes.  Nothing here needs the progress bar.
$ProgressPreference    = "SilentlyContinue"

$FAIL = [System.Collections.ArrayList]@()
$WARN = [System.Collections.ArrayList]@()
$script:RebootNeeded = $false

function Say($m)   { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($l,$d) { Write-Host "  [ok  ] $l" -NoNewline; if($d){Write-Host "  $d" -ForegroundColor DarkGray}else{Write-Host ""} }
function Bad($l,$d){ Write-Host "  [FAIL] $l" -NoNewline -ForegroundColor Red; if($d){Write-Host "  $d" -ForegroundColor DarkGray}else{Write-Host ""}; [void]$FAIL.Add($l) }
function Warn($l,$d){Write-Host "  [warn] $l" -NoNewline -ForegroundColor Yellow; if($d){Write-Host "  $d" -ForegroundColor DarkGray}else{Write-Host ""}; [void]$WARN.Add($l) }

function Update-SessionPath {
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path","User")
}

# Under $ErrorActionPreference = "Stop", Windows PowerShell 5.1 turns ANY stderr
# line from a native command into a terminating error, even with 2>$null.  Every
# native probe goes through this so a chatty stderr cannot abort the script.
function Invoke-Quiet([scriptblock]$sb) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $sb 2>$null } catch { $null } finally { $ErrorActionPreference = $old }
}

# ---------------------------------------------------------------- 0. preconditions
# Guard the GNU-style-flag mistake: PowerShell switches take ONE dash, so
# "--VerifyOnly" is not an unknown switch but a POSITIONAL argument that binds
# silently to $WorkspacesRoot.  The script then runs the mutating block it was
# asked to skip and writes a Defender exclusion for a path named "--VerifyOnly",
# with no error.
foreach ($p in @(@{n='WorkspacesRoot';v=$WorkspacesRoot},
                 @{n='AdvisorRoot'  ;v=$AdvisorRoot},
                 @{n='McpSubnet'    ;v=$McpSubnet})) {
  if ($p.v -like '-*') {
    Write-Host ""
    Write-Host "  -$($p.n) received '$($p.v)'." -ForegroundColor Red
    Write-Host "  PowerShell switches take ONE dash -- '--VerifyOnly' binds as a" -ForegroundColor Red
    Write-Host "  positional path instead of setting the switch." -ForegroundColor Red
    Write-Host ""
    Write-Host "    .\provision-vm.ps1 -VerifyOnly" -ForegroundColor Yellow
    Write-Host ""
    exit 2
  }
}

Say "preconditions"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Ok "running elevated" } else { Bad "not elevated" "re-run this in an Administrator PowerShell" }

if ([Environment]::Is64BitOperatingSystem) { Ok "64-bit Windows" ([Environment]::OSVersion.VersionString) }
else { Bad "32-bit Windows" "the Roslyn hosts need x64" }

# .NET Framework 4.8 runtime: in-box on Win10 1903+ / Server 2019+, but the net48
# host will not start without it, and the failure is opaque.
$ndp = "HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
$rel = (Get-ItemProperty $ndp -ErrorAction SilentlyContinue).Release
if ($rel -ge 528040) { Ok ".NET Framework 4.8+ runtime" "Release=$rel" }
else { Bad ".NET Framework 4.8 runtime missing" "Release=$rel; install the 4.8 runtime" }

# winget is how Python and the SDK get installed.  Only required when installing.
$haveWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
if ($haveWinget) { Ok "winget present" }
elseif ($VerifyOnly) { Warn "winget not found" "only needed when installing" }
else { Bad "winget not found" "install App Installer, or install Python 3.13 and SDK 9 by hand" }

# -WorkspacesRoot defaults to a placeholder, and -AdvisorRoot must be absolute:
# Defender exclusions and the reference assembly root for either otherwise help nobody.
if (-not $VerifyOnly) {
  foreach ($p in @(@{n='WorkspacesRoot';v=$WorkspacesRoot;d='YourWorkspacePath'},
                   @{n='AdvisorRoot'  ;v=$AdvisorRoot   ;d='ServerPath'})) {
    if ($p.v -eq $p.d -or -not [IO.Path]::IsPathRooted($p.v)) {
      Bad "-$($p.n) is '$($p.v)'" "pass the real absolute path, e.g. -$($p.n) C:\..."
    }
  }
}

# Measured on the dev box: the net48 compiler pass peaks at 3.7 GB while the
# graph and SQLite are also resident; .index/ is ~4 GB, the NuGet cache reaches
# ~17 GB on a first restore, and every project's bin/obj comes on top.
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
# A "16 GB" machine reports ~15.7 GB usable.
if ($ramGb -ge 15)    { Ok "memory" "$ramGb GB" }
elseif ($ramGb -ge 8) { Warn "memory $ramGb GB" "16 GB recommended: the net48 compiler pass peaks at 3.7 GB" }
else                  { Bad "memory $ramGb GB" "at least 8 GB (16 recommended) for the compiler index" }

$drives = @()
foreach ($p in @($WorkspacesRoot, $AdvisorRoot, $NuGetCache)) {
  if ([IO.Path]::IsPathRooted($p)) { $drives += ([IO.Path]::GetPathRoot($p)).Substring(0,1).ToUpper() }
}
foreach ($d in ($drives | Select-Object -Unique)) {
  $free = [math]::Round((Get-PSDrive $d).Free / 1GB, 1)
  if ($free -ge 40)     { Ok "free disk $($d):" "$free GB" }
  elseif ($free -ge 20) { Warn "free disk $($d): $free GB" "40 GB recommended (NuGet cache, bin/obj, .index)" }
  else                  { Bad "free disk $($d): $free GB" "at least 20 GB; 40 GB recommended" }
}

if ($FAIL.Count) { Write-Host ""; Write-Host "stopping: fix the above first." -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 1. egress preflight
# Admin on the box does not open the corporate proxy -- that is enforced upstream.
# Find out now, not four steps into a VS install.
$VsBootstrapperUrl = "https://aka.ms/vs/17/release/vs_BuildTools.exe"

if (-not $SkipEgressCheck) {
  Say "egress preflight"
  $egressWarn = 0
  foreach ($probe in @(
      @{ n = "nuget.org";       u = "https://api.nuget.org/v3/index.json" },
      @{ n = "pypi.org";        u = "https://pypi.org/simple/" },
      @{ n = "winget CDN";      u = "https://cdn.winget.microsoft.com/cache/source.msix" },
      @{ n = "VS Build Tools";  u = $VsBootstrapperUrl })) {
    try {
      $null = Invoke-WebRequest $probe.u -UseBasicParsing -TimeoutSec 20 -Method Head
      Ok "$($probe.n) reachable"
    } catch {
      Warn "$($probe.n) NOT reachable" $_.Exception.Message
      $egressWarn++
    }
  }
  # Optional: the second package source on the dev box (a user-level NuGet.Config).
  # It is unreachable there too; restore tolerates it (RestoreIgnoreFailedSources),
  # but the FastReport packages it serves then do not restore.
  try {
    $null = Invoke-WebRequest "https://nuget.fast-report.com/api/v3/index.json" -UseBasicParsing -TimeoutSec 20 -Method Head
    Ok "nuget.fast-report.com reachable" "add it to the pipeline account's NuGet.Config if FastReport projects must restore"
  } catch {
    Warn "nuget.fast-report.com NOT reachable" "optional; projects using FastReport packages will show as unclean"
  }
  if ($egressWarn) {
    Write-Host ""
    Write-Host "  Offline route if these stay blocked:" -ForegroundColor Yellow
    Write-Host "    nuget : copy %USERPROFILE%\.nuget\packages from the dev box" -ForegroundColor DarkGray
    Write-Host "    pypi  : pip download -d appdb -r <deps> on the dev box, then" -ForegroundColor DarkGray
    Write-Host "            pip install --no-index --find-links appdb -e `".[dev]`"" -ForegroundColor DarkGray
    Write-Host "    vs    : use the VS offline layout (--layout)" -ForegroundColor DarkGray
    Write-Host ""
  }
}

# ---------------------------------------------------------------- 2. machine settings
if (-not $VerifyOnly) {
  Say "machine settings"

  # The build scripts are .ps1 and arrive copied from another machine.
  $localPolicy = Get-ExecutionPolicy -Scope LocalMachine

  if ($localPolicy -ne 'RemoteSigned') {
    Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
  }

  Ok "execution policy" "LocalMachine = $(Get-ExecutionPolicy -Scope LocalMachine)"

  # TFVC roots plus deep App/ paths overrun MAX_PATH.
  $fsKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
  $lp    = (Get-ItemProperty $fsKey -ErrorAction SilentlyContinue).LongPathsEnabled
  if ($lp -eq 1) {
    Ok "long paths enabled" "already set"
  } else {
    New-ItemProperty -Path $fsKey -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null
    $script:RebootNeeded = $true
    Ok "long paths enabled" "takes effect on reboot"
  }

  # Real-time scanning adds minutes to every index pass (6,357 files, a 500 MB
  # WAL database), and the build step adds ~700 MSBuild runs, their compilers, a
  # full NuGet restore and ASP.NET's page compiler on top.
  try {
    foreach ($p in @($WorkspacesRoot, (Join-Path $AdvisorRoot ".index"), $NuGetCache)) {
      Add-MpPreference -ExclusionPath $p -ErrorAction Stop
    }
    $procs = 'RoslynHost.exe','python.exe','MSBuild.exe','dotnet.exe','VBCSCompiler.exe','MarkupCodegen.exe'
    Add-MpPreference -ExclusionProcess $procs -ErrorAction Stop
    Ok "Defender exclusions" "$WorkspacesRoot, .index, $NuGetCache; $($procs -join ', ')"
  } catch {
    Warn "Defender exclusions not applied" $_.Exception.Message
  }

  # A ~6 minute index pass and a long-lived server should not meet standby.
  powercfg /change standby-timeout-ac 0   | Out-Null
  powercfg /change hibernate-timeout-ac 0 | Out-Null
  Ok "power: no standby/hibernate on AC"

  # There is no authentication on this server and DNS-rebinding protection is off
  # (README).  Scope the rule; never -RemoteAddress Any.  An existing rule is
  # re-scoped rather than trusted, so a changed -McpSubnet actually takes effect.
  $ruleName = "Senior-Dev-MCP $McpPort"
  if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
      -RemoteAddress $McpSubnet | Out-Null
  } else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
      -LocalPort $McpPort -Action Allow -RemoteAddress $McpSubnet | Out-Null
  }
  Ok "firewall inbound $McpPort" "restricted to $McpSubnet"
}

# ---------------------------------------------------------------- 3. Python 3.13
Say "Python 3.13"

function Get-Py313 {
  # py launcher first: it sees per-machine installs the current PATH may not.
  $c = @()
  $c += Invoke-Quiet { py -3.13 -c "import sys;print(sys.executable)" }
  $c += "C:\Program Files\Python313\python.exe"
  foreach ($p in ($c | Where-Object { $_ -and (Test-Path $_) })) {
    $v = Invoke-Quiet { & $p -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" }
    if ($v -like "3.13.*") { return [pscustomobject]@{ Path=$p; Version=$v } }
  }
  return $null
}

$py = Get-Py313
if (-not $py -and -not $VerifyOnly) {
  # python.org, machine scope -- NOT the Store build (different Scripts/ layout)
  # and NOT miniconda (the dev box uses it, but activation semantics are noise
  # for a service account).  The project venv is a plain venv either way.
  # --source winget skips the msstore source and its interactive agreement prompt.
  winget install --id Python.Python.3.13 --exact --source winget --scope machine `
                 --accept-package-agreements --accept-source-agreements
  $pyExit = $LASTEXITCODE
  Update-SessionPath
  $py = Get-Py313
  if (-not $py) { Warn "winget python install" "exit $pyExit" }
}
if ($py) {
  Ok "python 3.13" "$($py.Version)  $($py.Path)"
  if ($py.Version -ne "3.13.11") { Warn "dev box is 3.13.11" "$($py.Version) here; fine, but note the drift" }
} else { Bad "python 3.13 not found" "install python.org 3.13 x64, machine scope" }

# ---------------------------------------------------------------- 4. .NET SDK 9
Say ".NET SDK 9"

function Get-Sdk9 {
  return (Invoke-Quiet { dotnet --list-sdks } | Where-Object { $_ -match '^9\.' })
}
$sdk9 = Get-Sdk9
if (-not $sdk9 -and -not $VerifyOnly) {
  winget install --id Microsoft.DotNet.SDK.9 --exact --source winget --scope machine `
                 --accept-package-agreements --accept-source-agreements
  $sdkExit = $LASTEXITCODE
  Update-SessionPath
  $sdk9 = Get-Sdk9
  if (-not $sdk9) { Warn "winget SDK 9 install" "exit $sdkExit" }
}
if ($sdk9) { Ok ".NET SDK 9" (($sdk9 | Select-Object -First 1).Trim()) }
else       { Bad ".NET SDK 9 not found" "winget install Microsoft.DotNet.SDK.9 --scope machine" }

# ---------------------------------------------------------------- 5. Visual Studio 17.14
Say "Visual Studio / MSBuild"

$vswhere   = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsSetup   = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe"
$refRoot   = "${env:ProgramFiles(x86)}\Reference Assemblies\Microsoft\Framework\.NETFramework"
$ref472    = Join-Path $refRoot "v4.7.2"
$ref48     = Join-Path $refRoot "v4.8"
$ref481    = Join-Path $refRoot "v4.8.1"
$btProduct = "Microsoft.VisualStudio.Product.BuildTools"

# ManagedDesktopBuildTools carries the targeting packs; WebBuildTools carries
# Microsoft.WebApplication.targets, without which the legacy web projects in
# Legacy.sln do not load at all.
$vsComponents = @(
  "Microsoft.VisualStudio.Workload.MSBuildTools",
  "Microsoft.VisualStudio.Workload.ManagedDesktopBuildTools",
  "Microsoft.VisualStudio.Workload.WebBuildTools",
  # The .NET SDK resolver: without it VS MSBuild cannot evaluate any SDK-style
  # project (MSB4236), and build-workspace.py builds every project with VS MSBuild.
  "Microsoft.VisualStudio.Workload.NetCoreBuildTools",
  # ...the resolver itself (package Microsoft.Net.Core.SDK.MSBuildExtensions)
  # ships with this component, which the workload does NOT include: a Build Tools
  # install with the workload alone has no SdkResolvers\Microsoft.DotNet.MSBuildSdkResolver.
  "Microsoft.NetCore.Component.SDK",
  "Microsoft.Component.MSBuild",
  "Microsoft.Net.Component.4.7.2.TargetingPack",
  "Microsoft.Net.Component.4.8.SDK",
  "Microsoft.Net.Component.4.8.TargetingPack",
  # 5 projects target 4.8.1; step 7 copies it into the reference assembly root.
  "Microsoft.Net.Component.4.8.1.TargetingPack"
)
$vsAddArgs = @(); foreach ($c in $vsComponents) { $vsAddArgs += @("--add", $c) }

function Get-VsMSBuild {
  if (-not (Test-Path $vswhere)) { return $null }
  # -products * is what makes Build Tools visible: without it vswhere silently
  # excludes Build Tools products and this reports a false negative.
  # -version [17,18): the net48 Roslyn host is built against MSBuild 17.14 and
  # loads VS's MSBuild in-process; VS 18's MSBuild needs newer runtime assemblies
  # than the host ships (System.Collections.Immutable FrozenSet MissingMethodException).
  # So a VS 18 install does not count, and Build Tools 17.14 is installed beside it.
  $q = @("-latest","-products","*","-version","[17.0,18.0)","-requires","Microsoft.Component.MSBuild")
  $inst = Invoke-Quiet { & $vswhere @q -property installationPath }            | Select-Object -First 1
  if (-not $inst) { return $null }
  $ver  = Invoke-Quiet { & $vswhere @q -property catalog_productDisplayVersion } | Select-Object -First 1
  $prod = Invoke-Quiet { & $vswhere @q -property productId }                     | Select-Object -First 1
  [pscustomobject]@{
    Path       = $inst
    Version    = $ver
    Product    = $prod
    Bin        = Join-Path $inst "MSBuild\Current\Bin"
    # v* not v17.0: the folder is v<VisualStudioVersion> (v18.0 under VS 2026), and
    # Test-Path accepts the wildcard.
    WebTargets = Join-Path $inst "MSBuild\Microsoft\VisualStudio\v*\WebApplications\Microsoft.WebApplication.targets"
  }
}

function Get-BuildGaps($vs) {
  $g = @()
  if (-not $vs -or -not (Test-Path $vs.Bin)) { $g += "MSBuild" }
  else {
    if (-not (Test-Path $vs.WebTargets)) { $g += "Microsoft.WebApplication.targets" }
    if (-not (Test-Path (Join-Path $vs.Bin "SdkResolvers\Microsoft.DotNet.MSBuildSdkResolver"))) { $g += ".NET SDK resolver" }
  }
  if (-not (Test-Path $ref472)) { $g += "4.7.2 targeting pack" }
  if (-not (Test-Path $ref48))  { $g += "4.8 targeting pack" }
  if (-not (Test-Path $ref481)) { $g += "4.8.1 targeting pack" }
  return ,$g
}

function Invoke-VsInstaller([string]$exe, [string[]]$argv, [string]$what) {
  Write-Host "  running $what (quiet; typically 10-30 minutes)..." -ForegroundColor DarkGray
  # Start-Process -Wait, not '&': the bootstrapper is a GUI-subsystem exe, so '&'
  # can return before it finishes and $LASTEXITCODE is then meaningless.
  $proc = Start-Process -FilePath $exe -ArgumentList $argv -Wait -PassThru
  switch ($proc.ExitCode) {
    0       { Ok "$what finished" }
    3010    { Ok "$what finished" "exit 3010: installed, reboot required"; $script:RebootNeeded = $true }
    default { Bad "$what exit $($proc.ExitCode)" "logs: $env:TEMP\dd_bootstrapper_*.log, dd_setup_*.log" }
  }
}

$vs   = Get-VsMSBuild
$gaps = Get-BuildGaps $vs

if ($gaps.Count -and -not $VerifyOnly -and -not $SkipVsInstall) {
  Write-Host "  missing: $($gaps -join ', ')" -ForegroundColor DarkGray
  if (-not $vs) {
    $boot = Join-Path $env:TEMP "vs_BuildTools.exe"
    try {
      Invoke-WebRequest $VsBootstrapperUrl -OutFile $boot -UseBasicParsing
      Invoke-VsInstaller $boot (@("--quiet","--wait","--norestart","--nocache") + $vsAddArgs) "VS Build Tools install"
    } catch {
      Bad "Build Tools bootstrapper download failed" $_.Exception.Message
    }
  } elseif ($vs.Product -eq $btProduct -and (Test-Path $vsSetup)) {
    # Quote the path: Start-Process joins -ArgumentList with spaces and does not
    # quote, and the install path contains "Program Files (x86)".
    Invoke-VsInstaller $vsSetup (@("modify","--installPath","`"$($vs.Path)`"",
                                   "--quiet","--norestart") + $vsAddArgs) "VS Build Tools modify"
  } else {
    Warn "not modifying $($vs.Product)" "add Workload.ManagedDesktop + Workload.NetWeb + the 4.7.2/4.8 targeting packs in Visual Studio Installer"
  }
  $vs = Get-VsMSBuild
}

$vsOk = $false
if ($vs) {
  if ($vs.Product) { Ok "product" $vs.Product }
  if (Test-Path $vs.Bin) {
    $vsOk = $true
    Ok "VS MSBuild found" "$($vs.Version)  $($vs.Bin)"
    # 17.14 is the LAST VS 2022 minor and stays on the Current channel until
    # Jan 2032: there is no 17.15 to drift into and no LTSC 17.14 channel, so the
    # plain bootstrapper is correct and the only possible drift is within 17.14.x.
    #
    # Patch drift is the tolerable kind: the known failure was a MINOR mismatch
    # (packages 17.13.9 vs assemblies 17.14) breaking every legacy project.
    # 17.14.8 packages against 17.14.x assemblies should be fine, but prove it
    # with -Verify before trusting that.
    if ($vs.Version -notlike "17.14.*") {
      Warn "VS is not 17.14" "$($vs.Version) -- set MSBuildPkgVersion in RoslynHost.csproj to match, then re-verify with start-hosts.ps1 -Verify"
    }
    if (Test-Path $vs.WebTargets) { Ok "Microsoft.WebApplication.targets present" }
    else { Bad "Microsoft.WebApplication.targets missing" "add Microsoft.VisualStudio.Workload.WebBuildTools; legacy web projects will not load" }
    $sdkResolver = Join-Path $vs.Bin "SdkResolvers\Microsoft.DotNet.MSBuildSdkResolver"
    if (Test-Path $sdkResolver) { Ok ".NET SDK resolver present" }
    else { Bad ".NET SDK resolver missing" "add Microsoft.NetCore.Component.SDK (re-run this script without -VerifyOnly); build-workspace.py cannot build SDK-style projects (MSB4236)" }
    # build-hosts.ps1 copies this out of the VS bin to repair BuildHost-net472.
    $stx = Join-Path $vs.Bin "System.Threading.Tasks.Extensions.dll"
    if (Test-Path $stx) {
      $v = [System.Reflection.AssemblyName]::GetAssemblyName($stx).Version.ToString()
      Ok "BuildHost repair source present" "System.Threading.Tasks.Extensions $v"
    } else { Warn "System.Threading.Tasks.Extensions.dll absent from VS bin" "build-hosts.ps1 will throw" }
  } else { Bad "MSBuild\Current\Bin missing under $($vs.Path)" }
}
if (-not $vsOk) {
  Bad "no Visual Studio with MSBuild" "legacy (.NET Framework) projects will not load at all"
  Write-Host ""
  Write-Host "  Install BUILD TOOLS, not the IDE: this is a headless server, and" -ForegroundColor Yellow
  Write-Host "  Build Tools is free for any org (no Community licensing question)." -ForegroundColor Yellow
  Write-Host "  Re-run this script without -VerifyOnly to install it, or by hand:" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "    `$ProgressPreference = 'SilentlyContinue'"                             -ForegroundColor DarkGray
  Write-Host "    Invoke-WebRequest $VsBootstrapperUrl -OutFile `$env:TEMP\vs_BuildTools.exe" -ForegroundColor DarkGray
  Write-Host "    `$p = Start-Process `$env:TEMP\vs_BuildTools.exe -Wait -PassThru -ArgumentList @(" -ForegroundColor DarkGray
  Write-Host "      '--quiet','--wait','--norestart',"                                  -ForegroundColor DarkGray
  foreach ($c in $vsComponents) {
    $sep = if ($c -eq $vsComponents[-1]) { "" } else { "," }
    Write-Host "      '--add','$c'$sep"                                                 -ForegroundColor DarkGray
  }
  Write-Host "    )"                                                                    -ForegroundColor DarkGray
  Write-Host "    `$p.ExitCode   # 0 or 3010 (= reboot required) is success"            -ForegroundColor DarkGray
  Write-Host ""
  Write-Host "  Build Tools has not been proven against the in-process net48 path"      -ForegroundColor Yellow
  Write-Host "  on this project. start-hosts.ps1 -Verify is the decisive test:"         -ForegroundColor Yellow
  Write-Host "  expect net48 8/8 + 815 website docs. If it fails there, install the"    -ForegroundColor Yellow
  Write-Host "  full IDE (Workload.ManagedDesktop + Workload.NetWeb) instead."          -ForegroundColor Yellow
  Write-Host ""
}

# ---------------------------------------------------------------- 6. targeting packs
Say "reference assemblies"

if (Test-Path $ref472) {
  Ok "4.7.2 targeting pack" $ref472
  # gen_aspnet_controls.ps1 loads exactly these; System.Web alone is not enough
  # (ScriptManager/UpdatePanel/Timer live in System.Web.Extensions).
  foreach ($dll in @("System.Web.dll","System.Web.Extensions.dll","System.Web.Mobile.dll")) {
    if (Test-Path (Join-Path $ref472 $dll)) { Ok "  $dll" }
    elseif ($dll -eq "System.Web.Mobile.dll") { Warn "  $dll absent" "optional" }
    else { Bad "  $dll absent" "gen_aspnet_controls.ps1 cannot build the control table" }
  }
} else {
  Bad "4.7.2 targeting pack missing" "add Microsoft.Net.Component.4.7.2.TargetingPack"
}

if (Test-Path $ref48) { Ok "4.8 targeting pack" $ref48 }
else { Bad "4.8 targeting pack missing" "add Microsoft.Net.Component.4.8.TargetingPack; the net48 host target will not compile" }

if (Test-Path $ref481) { Ok "4.8.1 targeting pack" $ref481 }
else { Bad "4.8.1 targeting pack missing" "add Microsoft.Net.Component.4.8.1.TargetingPack; 5 projects target 4.8.1" }

# Windows PowerShell 5.1 specifically: gen_aspnet_controls.ps1 reflects over
# .NET Framework assemblies and will not do that under pwsh 7 (.NET Core).
if (Test-Path "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe") {
  Ok "Windows PowerShell 5.1 present" "gen_aspnet_controls.ps1 needs it, not pwsh 7"
} else { Bad "Windows PowerShell 5.1 missing" }

# ---------------------------------------------------------------- 7. reference assembly root
# One folder in the Microsoft.NETFramework.ReferenceAssemblies layout holding every
# .NET Framework version the workspace targets; the build and the Roslyn load both
# use it as TargetFrameworkRootPath.  4.5 - 4.7.1 come from Microsoft's signed
# NuGet packages (author signature checked), 4.7.2 - 4.8.1 from the targeting packs.
Say "reference assembly root (.NET Framework 4.5 - 4.8.1)"

$refasmRoot = Join-Path $AdvisorRoot ".index\build\refasm"
$refasm     = Join-Path $refasmRoot ".NETFramework"
$fromNuGet  = [ordered]@{ "v4.5"="net45"; "v4.5.2"="net452"; "v4.6"="net46"; "v4.6.1"="net461"; "v4.6.2"="net462"; "v4.7.1"="net471" }
$fromPacks  = @("v4.7.2", "v4.8", "v4.8.1")
function Test-RefDir([string]$d) { Test-Path (Join-Path $d "mscorlib.dll") }

if (-not $VerifyOnly -and [IO.Path]::IsPathRooted($AdvisorRoot)) {
  New-Item -ItemType Directory -Force $refasm | Out-Null
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  foreach ($v in $fromNuGet.Keys) {
    if (Test-RefDir (Join-Path $refasm $v)) { continue }
    $id  = "microsoft.netframework.referenceassemblies.$($fromNuGet[$v])"
    $pkg = Join-Path $env:TEMP "$id.1.0.3.nupkg"
    try {
      Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/$id/1.0.3/$id.1.0.3.nupkg" -OutFile $pkg -UseBasicParsing
      $out = Invoke-Quiet { dotnet nuget verify --all $pkg }
      if ($LASTEXITCODE -ne 0 -or -not (($out -join "`n") -match 'CN=Microsoft Corporation')) {
        Bad "$id signature" "dotnet nuget verify did not confirm a Microsoft author signature; not extracted"
        continue
      }
      $zip = [IO.Compression.ZipFile]::OpenRead($pkg)
      try {
        foreach ($e in $zip.Entries) {
          if ($e.FullName -like "build/.NETFramework/*" -and $e.Name) {
            $dst = Join-Path $refasmRoot ($e.FullName.Substring("build/".Length) -replace '/', '\')
            New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dst, $true)
          }
        }
      } finally { $zip.Dispose() }
    } catch {
      Bad "$v reference assemblies" $_.Exception.Message
    }
  }
  foreach ($v in $fromPacks) {
    $dst = Join-Path $refasm $v
    $src = Join-Path $refRoot $v
    if (-not (Test-RefDir $dst) -and (Test-Path $src)) { Copy-Item $src $dst -Recurse -Force }
  }
}
foreach ($v in @($fromNuGet.Keys) + $fromPacks) {
  $d = Join-Path $refasm $v
  if (Test-RefDir $d) { Ok "$v" $d }
  else { Bad "$v reference assemblies missing" "re-run without -VerifyOnly (needs nuget.org and the VS targeting packs)" }
}
Write-Host "  config.toml [build] target_framework_root: leave empty -- this folder is the default, and build-workspace fills it by itself" -ForegroundColor DarkGray

# ---------------------------------------------------------------- 8. ASP.NET page compiler
# markup-codegen asks ASP.NET's own page compiler (System.Web.Compilation.
# ClientBuildManager) for every page's generated code.  That needs System.Web from
# the installed .NET Framework, not IIS.  A Windows client has it without any
# ASP.NET feature enabled (the dev box has none registered); Windows Server may
# need the ASP.NET 4.8 feature.  Test first.
Say "ASP.NET page compiler (markup-codegen)"

function Test-ClientBuildManager {
  try {
    Add-Type -AssemblyName System.Web -ErrorAction Stop
    return [bool]([Type]::GetType("System.Web.Compilation.ClientBuildManager, System.Web, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a"))
  } catch { return $false }
}

if ($PSVersionTable.PSEdition -ne 'Desktop') {
  Warn "cannot check from PowerShell $($PSVersionTable.PSVersion)" "run this script in Windows PowerShell 5.1"
} elseif (Test-ClientBuildManager) {
  Ok "System.Web ClientBuildManager loads" "page code generation can run"
} elseif (-not $VerifyOnly) {
  $productType = (Get-CimInstance Win32_OperatingSystem).ProductType   # 1 = client, 2/3 = server
  try {
    if ($productType -eq 1) {
      Enable-WindowsOptionalFeature -Online -FeatureName NetFx4Extended-ASPNET45 -All -NoRestart -ErrorAction Stop | Out-Null
    } else {
      Install-WindowsFeature NET-Framework-45-ASPNET -ErrorAction Stop | Out-Null
    }
    Ok "ASP.NET 4.8 feature enabled" "a new PowerShell session may be needed to load it"
  } catch {
    Bad "System.Web ClientBuildManager does not load" "enable ASP.NET 4.8 (Server: NET-Framework-45-ASPNET) by hand: $($_.Exception.Message)"
  }
} else {
  Bad "System.Web ClientBuildManager does not load" "re-run without -VerifyOnly to enable ASP.NET 4.8, or markup falls back to synthesised designers"
}

# ---------------------------------------------------------------- 9. summary
Write-Host ""
Say "summary"
if ($script:RebootNeeded) {
  Write-Host "  REBOOT REQUIRED before building (long paths and/or VS installer 3010)." -ForegroundColor Yellow
  Write-Host "  Then: .\provision-vm.ps1 -VerifyOnly" -ForegroundColor Yellow
  Write-Host ""
}
if ($FAIL.Count) {
  Write-Host "  FAILED: $($FAIL.Count) -- $($FAIL -join ', ')" -ForegroundColor Red
  Write-Host ""
  Write-Host "  Fix these, then re-run with -VerifyOnly." -ForegroundColor Red
  exit 1
}
if ($WARN.Count) { Write-Host "  OK with $($WARN.Count) note(s) -- $($WARN -join ', ')" -ForegroundColor Yellow }
else             { Write-Host "  OK -- the machine is ready." -ForegroundColor Green }

Write-Host ""
Write-Host "  Next, in order:" -ForegroundColor Cyan
Write-Host "    1. set up the scheduled job that gets latest into $WorkspacesRoot"
Write-Host "       (as the SERVICE account that will run the pipeline -- TFVC mappings"
Write-Host "       are per-user per-machine, and so is its NuGet cache)"
Write-Host "    2. unzip mcp.zip so that $AdvisorRoot is its code_advisor folder, then:"
Write-Host "         Get-ChildItem -Recurse (Split-Path -Parent $AdvisorRoot) | Unblock-File"
Write-Host "       (.index\build\refasm from step 7 above is already in place)"
Write-Host "    3. edit $AdvisorRoot\config.toml for this machine:"
Write-Host "         [paths]  workspaces, net48_solution, net8_solution, website_root"
Write-Host "                  (everything else is relative to the advisor folder)"
Write-Host "         [build]  target_framework_root = `"`"   (empty: .index\build\refasm)"
if ($vs -and $vs.Bin) {
  Write-Host "                  msbuild = `"$((Join-Path $vs.Bin 'MSBuild.exe') -replace '\\','/')`""
  Write-Host "                  (VS 2022 / 17.x; required when a VS 18 install is also present)"
}
Write-Host "    4. $AdvisorRoot> powershell -ExecutionPolicy Bypass -File code_advisor_pipeline.ps1"
Write-Host "       (setup -> build hosts -> build workspace -> controls -> reindex -> tests;"
Write-Host "       ~20 min, first run longer: full NuGet restore).  Some project failures are"
Write-Host "       normal -- see .index\build\manifest.json; 'missing_targeting_packs' must be empty."
Write-Host "    5. powershell -ExecutionPolicy Bypass -File scripts\start-hosts.ps1 -Verify"
Write-Host "       then with the live hosts:  `$env:SENIOR_DEV_ROSLYN='1'; .venv\Scripts\python -m pytest tests\ -q"
Write-Host "    6. powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
Write-Host ""
exit 0