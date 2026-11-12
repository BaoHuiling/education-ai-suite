# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Runs the cross-model evaluation (python -m components.context_bench.evaluate) with the
# backend venv's Python from the Smart Classroom root: performance and accuracy of the INT4
# models, then one Markdown report. By default every profile of every config_*.yaml runs, so
# each config's documented test steps are all covered (throughput, classroom and accuracy):
#
#   Model             Profiles
#   qwen3-vl-8b       paged_min, eagle3_k3, eagle3_k4, eagle3_k5     (Eagle3 off and on)
#   qwen3.5-9b        paged_min, dflash_k3, dflash_k5, dflash_k7, dflash_k15   (DFlash off and on)
#   qwen3.6-35b-a3b   paged_min, dflash_k3, dflash_k5, dflash_k7, dflash_k15   (DFlash off and on)
#   qwen3.8-27b       paged_min
#
# Every argument is forwarded; its exit code (0 ok, 1 failed/partial cases, 2 invalid
# arguments, 130 Ctrl-C) is this script's exit code.
#
# Usage:
#   .\components\context_bench\run_evaluation.ps1 --list-profiles
#   .\components\context_bench\run_evaluation.ps1 --quick
#   .\components\context_bench\run_evaluation.ps1
#   .\components\context_bench\run_evaluation.ps1 --spec-k 5 7          # paged_min + only k=5/7
#   .\components\context_bench\run_evaluation.ps1 --models qwen3-vl-8b qwen3.5-9b --suites throughput
#
# If PowerShell blocks local scripts, relax the policy for this process only:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

# Two directories above this script: context_bench -> components -> app root
# (smart-classroom/Windows).
$SmartClassroomRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir '..\..')).ProviderPath
$VenvPath = Join-Path (Split-Path -Parent $SmartClassroomRoot) 'smartclassroom'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host "Backend venv not found at $VenvPath." -ForegroundColor Red
    Write-Host "Prepare it first from the desktop app's Setup screen: $(Join-Path $SmartClassroomRoot 'start-desktop-app.ps1')" -ForegroundColor Yellow
    # DEPRECATED(webui): drop this line after 2027.0.0.
    Write-Host "  (or the deprecated $(Join-Path $SmartClassroomRoot 'setup-smart-classroom.ps1'))" -ForegroundColor Yellow
    exit 2
}

$ExitCode = 1
Push-Location -LiteralPath $SmartClassroomRoot
try {
    & $VenvPython -m components.context_bench.evaluate @($args | ForEach-Object { [string]$_ })
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $ExitCode
