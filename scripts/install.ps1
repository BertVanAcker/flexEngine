# flexEngine installer for Windows
#
# Usage (PowerShell):
#   iwr -useb https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.ps1 | iex
#
# To pin a version or pass options, download the script first:
#   iwr -useb .../install.ps1 -OutFile install.ps1
#   .\install.ps1 -Version 0.2.0 -Workspace C:\my-agent -Config C:\my-config.yaml
#
# Docker is optional and only needed if you plan to deploy applications as
# containers. When run interactively you'll be asked whether to install it;
# for scripted/non-interactive installs pass -InstallDocker or -NoDocker
# explicitly (the default is to skip it).
#
# Installs flexEngine into an isolated venv (no admin rights, no system-wide
# changes) and scaffolds an agent workspace via `flexEngine init`.

param(
    [string]$Version = "",
    [string]$Workspace = ".\flexEngine-workspace",
    [string]$Config = "",
    [switch]$InstallDocker,
    [switch]$NoDocker
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

Write-Host ""
$installDocker = $null
if ($InstallDocker) { $installDocker = $true }
elseif ($NoDocker) { $installDocker = $false }

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "Docker is already installed, skipping."
} else {
    if ($null -eq $installDocker) {
        if (-not [Console]::IsInputRedirected) {
            $reply = Read-Host "Docker is optional (only needed to deploy applications as containers). Install it now? [y/N]"
            $installDocker = $reply -match '^[Yy]'
        } else {
            # No terminal attached to ask (e.g. run via iwr | iex) - default to skip.
            $installDocker = $false
        }
    }

    if ($installDocker) {
        Write-Host "Installing Docker Desktop ..."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Docker.DockerDesktop
            Write-Host "note: launch Docker Desktop once to finish setup and start the daemon."
        } else {
            Write-Host "note: winget not found, can't install Docker Desktop automatically."
            Write-Host "      Download it from: https://www.docker.com/products/docker-desktop/"
        }
    } else {
        Write-Host "Skipping Docker install. You can install it later: https://docs.docker.com/get-docker/"
    }
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
