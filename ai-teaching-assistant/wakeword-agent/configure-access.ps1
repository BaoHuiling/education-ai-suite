# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

[CmdletBinding()]
param(
    [string]$Path = (Join-Path $PSScriptRoot 'wake-access.local.json'),
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw 'Private access configuration not found. Copy wake-access.example.json to wake-access.local.json and enter your Microsoft-approved values locally.'
}
if ((Get-Item -LiteralPath $Path).Length -eq 0 -or (Get-Item -LiteralPath $Path).Length -gt 16384) {
    throw 'Private access configuration must be a nonempty JSON file no larger than 16 KiB.'
}
try {
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    $json = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path).ProviderPath, $encoding)
    if (-not $json.TrimStart().StartsWith('{')) {
        throw 'Configuration must be a JSON object.'
    }
    $configuration = $json | ConvertFrom-Json
}
catch {
    throw 'Private access configuration is not valid UTF-8 JSON. Its contents have not been logged.'
}
foreach ($name in @('token', 'attestation')) {
    $value = $configuration.$name
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value) -or $value.TrimStart().StartsWith('REPLACE_WITH_', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Private access configuration requires a non-placeholder string for '$name'."
    }
}

if ($ValidateOnly) {
    Write-Host '[OK] Access configuration format is valid. Microsoft authorization is checked when the installed host starts.'
    return
}

$manifest = [xml][System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Package.appxmanifest'))
$packages = @(Get-AppxPackage -Name $manifest.Package.Identity.Name | Where-Object { $_.Publisher -eq $manifest.Package.Identity.Publisher })
if ($packages.Count -ne 1) {
    throw 'Install the matching ATA wake package for the signed-in user before configuring access.'
}
$localState = Join-Path $env:LOCALAPPDATA "Packages\$($packages[0].PackageFamilyName)\LocalState"
$destination = Join-Path $localState 'wake-access.local.json'
[void][System.IO.Directory]::CreateDirectory($localState)
if ([System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).ProviderPath) -ne [System.IO.Path]::GetFullPath($destination)) {
    Copy-Item -LiteralPath $Path -Destination $destination -Force
}
Write-Host '[OK] Private access configuration saved for the installed package. No credential values were printed.'
Write-Host '[*] From the ATA directory, run start_wakeword.ps1 again to reload the configuration.'