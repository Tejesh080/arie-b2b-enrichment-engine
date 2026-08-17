<#
.SYNOPSIS
    One-command local demo of ARIE's Decision Receipt.

.DESCRIPTION
    Thin Windows launcher only -- all the logic lives in scripts/demo/, a
    portable Python package (see its own module docstrings). This script's
    only job is to resolve a Python interpreter and forward arguments.

.PARAMETER Fresh
    Wipe local Docker volumes first (docker compose down -v) before starting
    the demo. Destructive -- deletes local ARIE demo data. Opt-in only; the
    default run never does this.

.EXAMPLE
    .\scripts\demo.ps1

.EXAMPLE
    .\scripts\demo.ps1 -Fresh
#>
param(
    [switch]$Fresh,
    [string]$BaseUrl,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

$demoArgs = @()
if ($Fresh) { $demoArgs += "--fresh" }
if ($BaseUrl) { $demoArgs += "--base-url"; $demoArgs += $BaseUrl }
if ($OutputDir) { $demoArgs += "--output-dir"; $demoArgs += $OutputDir }

Push-Location $repoRoot
try {
    & $python -m scripts.demo.cli @demoArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
