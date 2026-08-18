<#
.SYNOPSIS
    Generate ARIE's Policy Lab report — a Pareto visualization of the frozen
    M0 benchmark's policy trade-offs.

.DESCRIPTION
    Thin Windows launcher only -- all the logic lives in scripts/policy_lab/,
    a portable, unit-tested Python package. This script's only job is to
    resolve a Python interpreter and forward arguments.

    By default this reads the already-frozen bench/out/multi_seed.json
    artifact and does NOT run the benchmark. If that artifact doesn't exist
    yet (e.g. a fresh clone -- bench/out/ is gitignored), pass -Regenerate to
    run `python -m bench.multi_seed` first (~15 minutes, offline).

.EXAMPLE
    .\scripts\policy-lab.ps1

.EXAMPLE
    .\scripts\policy-lab.ps1 -Regenerate
#>
param(
    [switch]$Regenerate,
    [string]$Artifact,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

$labArgs = @()
if ($Regenerate) { $labArgs += "--regenerate" }
if ($Artifact) { $labArgs += "--artifact"; $labArgs += $Artifact }
if ($OutputDir) { $labArgs += "--output-dir"; $labArgs += $OutputDir }

Push-Location $repoRoot
try {
    & $python -m scripts.policy_lab.cli @labArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
