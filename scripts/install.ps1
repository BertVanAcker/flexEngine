# flexEngine installer for Windows
#
# Usage (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.ps1 | iex
#
# To pin a version or pass options, download the script first:
#   iwr -useb .../install.ps1 -OutFile install.ps1
#   .\install.ps1 -Version 0.2.0 -Workspace C:\my-agent -Config C:\my-config.yaml
#
# Installs flexEngine into an isolated venv (no admin rights, no system-wide
# changes) and scaffolds an agent workspace via `flexEngine init`.

param(
    [string]$Version = "",
    [string]$Workspace = ".\flexEngine-workspace",
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"

function Get-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            return $cmd
        }
    }
    return $null
}

$pythonCmd = Get-Python
if (-not $pythonCmd) {
    Write-Error "python not found. Install Python 3.10+ from https://www.python.org/downloads/windows/ and re-run this script."
    exit 1
}

$launchArgs = @()
if ($pythonCmd -eq "py") { $launchArgs = @("-3") }

$versionOutput = & $pythonCmd @launchArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
$parts = $versionOutput.Split(".")
$major = [int]$parts[0]
$minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Error "flexEngine requires Python 3.10+, found $versionOutput."
    exit 1
}

$installDir = if ($env:FLEXENGINE_INSTALL_DIR) { $env:FLEXENGINE_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "flexEngine" }
$venvDir = Join-Path $installDir "venv"
$binDir = Join-Path $installDir "bin"

Write-Host "Installing flexEngine into an isolated environment at $venvDir ..."
& $pythonCmd @launchArgs -m venv $venvDir

$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"
$venvFlexEngine = Join-Path $venvDir "Scripts\flexEngine.exe"

$pipSpec = "flexEngine"
if ($Version -ne "") {
    $pipSpec = "flexEngine==$Version"
}

& $venvPython -m pip install --quiet --upgrade pip
& $venvPip install --quiet $pipSpec

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$shimPath = Join-Path $binDir "flexEngine.cmd"
Set-Content -Path $shimPath -Value "@echo off`r`n`"$venvFlexEngine`" %*"

Write-Host "flexEngine installed."

if (($env:Path -split ";") -notcontains $binDir) {
    Write-Host ""
    Write-Host "note: $binDir is not on your PATH. Add it, e.g.:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', `$env:Path + ';$binDir', 'User')"
}

$initArgs = @($Workspace)
if ($Config -ne "") {
    $initArgs += @("--config", $Config)
}
if ([Console]::IsInputRedirected) {
    $initArgs += "--non-interactive"
}

Write-Host ""
Write-Host "Setting up workspace at $Workspace ..."
& $venvFlexEngine init @initArgs
