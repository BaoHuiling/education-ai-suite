# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Runs the context benchmark with the backend venv's Python from the Smart Classroom root.
# Every argument is forwarded to `python -m components.context_bench.benchmark`, and its
# exit code (0 ok, 1 failed/partial cases, 2 configuration or dependencies, 130 Ctrl-C) is
# this script's exit code.
#
# A relative --config is resolved against the caller's directory before changing to the
# Smart Classroom root; model, cache and output paths resolve from that root.
#
# Usage:
#   .\components\context_bench\run_benchmark.ps1 --list-profiles
#   .\components\context_bench\run_benchmark.ps1 --config components/context_bench/config_qwen3.8_27b.yaml
#   .\components\context_bench\run_benchmark.ps1 --profiles paged_min dflash_k7 --contexts 8000 --warmup 0 --iterations 1
#
# The venv (<app root>/../smartclassroom) is not created here: the desktop app's Setup
# screen (start-desktop-app.ps1) or setup-smart-classroom.ps1 creates it with the
# application's requirements.txt, which pins everything the benchmark needs.
# DEPRECATED(webui): drop the setup-smart-classroom.ps1 mentions after 2027.0.0.
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
    Write-Host "  (or the deprecated $(Join-Path $SmartClassroomRoot 'setup-smart-classroom.ps1'))" -ForegroundColor Yellow
    exit 2
}

$CallerDir = (Get-Location -PSProvider FileSystem).ProviderPath

function Resolve-CallerPath([string]$Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return [System.IO.Path]::GetFullPath((Join-Path $CallerDir $Path))
}

# Forward every argument as a string; only a relative --config value is rewritten.
$Forward = New-Object System.Collections.Generic.List[string]
$Arguments = @($args)
for ($i = 0; $i -lt $Arguments.Count; $i++) {
    $Argument = [string]$Arguments[$i]
    if ($Argument -eq '--config' -and ($i + 1) -lt $Arguments.Count) {
        $Forward.Add($Argument)
        $i++
        $Forward.Add((Resolve-CallerPath ([string]$Arguments[$i])))
    }
    elseif ($Argument.StartsWith('--config=')) {
        $Forward.Add('--config=' + (Resolve-CallerPath $Argument.Substring(9)))
    }
    else {
        $Forward.Add($Argument)
    }
}

$ExitCode = 1
Push-Location -LiteralPath $SmartClassroomRoot
try {
    & $VenvPython -m components.context_bench.benchmark @($Forward.ToArray())
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $ExitCode
