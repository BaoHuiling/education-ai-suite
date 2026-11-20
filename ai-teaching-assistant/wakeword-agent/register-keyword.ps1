# =============================================================================
# register-keyword.ps1  -  bind the DSP keyword to the ATA Wake Agent through
# Windows Agent Activation Runtime configuration.
#
# With UseAarConfigFile=1, AarSvc builds its keyword->app bindings from
# C:\aarlog\aarconfig.txt and discards hand-written SignalDetectionConfigurations
# entries. Each line is "DisplayName,{ModelGuid},LCID,AUMID,background", and a
# model GUID may be bound to only ONE app, so binding ATA unbinds the previous
# owner. The previous file + registry are backed up first.
#
# Must run ELEVATED as the signed-in user (restarting AarSvc needs admin rights).
#
#   .\register-keyword.ps1                          # make ATA own the Cortana keyword
#   .\register-keyword.ps1 -Restore C:\aarlog\backup-<timestamp>
# =============================================================================
param(
    [string]$PackageName   = "Intel.ATA.WakeAgent",
    [string]$AppId         = "AtaWakeAgent",
    [string]$Keyword       = "Cortana",
    [string]$ModelId       = "{86FA368E-357C-408C-BFCF-B89CBA2B0006}",   # Cortana model
    [int]   $Lcid          = 1033,                                       # en-US
    [string]$AarConfigPath = "C:\aarlog\aarconfig.txt",
    [string]$Restore       = ""
)

$ErrorActionPreference = "Stop"
$RegPath = "HKCU\SOFTWARE\Microsoft\Speech_OneCore\Settings\VoiceActivation"
$Root    = "Registry::HKEY_CURRENT_USER\SOFTWARE\Microsoft\Speech_OneCore\Settings\VoiceActivation"

$elevated = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    throw "Run this script from an elevated PowerShell (AarSvc must be restarted)."
}
# AarSvc is per-user and HKCU must be the signed-in user's hive, not another admin account's.
$consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
if ($consoleUser -and ($consoleUser -split '\\')[-1] -ne $env:USERNAME) {
    throw "Elevated as '$env:USERNAME' but signed in as '$consoleUser'. Elevate with the signed-in account."
}

function Restart-Aar {
    $svc = Get-Service -Name "AarSvc_*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $svc) { throw "No per-user AarSvc_* service found." }
    Write-Host "[*] Restarting $($svc.Name)..." -ForegroundColor Cyan
    Restart-Service -Name $svc.Name -Force
    $svc.WaitForStatus("Running", [TimeSpan]::FromSeconds(20))
}

if ($Restore) {
    if (-not (Test-Path $Restore)) { throw "Backup folder not found: $Restore" }
    $savedConfig = Join-Path $Restore "aarconfig.txt"
    if (Test-Path $savedConfig) { Copy-Item $savedConfig $AarConfigPath -Force }
    $sdc = Join-Path $Root "SignalDetectionConfigurations"
    if (Test-Path $sdc) { Get-ChildItem $sdc | ForEach-Object { Remove-Item $_.PSPath -Recurse -Force } }
    reg.exe import (Join-Path $Restore "VoiceActivation.reg") | Out-Null
    Restart-Aar
    Write-Host "[OK] Restored aarconfig + voice-activation registry from $Restore" -ForegroundColor Green
    return
}

$pkg = Get-AppxPackage -Name $PackageName -ErrorAction SilentlyContinue
if (-not $pkg) {
    throw "Package '$PackageName' is not installed. Install the MSIX first (package.ps1 -Install)."
}
$aumid = "$($pkg.PackageFamilyName)!$AppId"
Write-Host "[*] Target app: $aumid" -ForegroundColor Cyan

# --- 1. Back up the current binding file and voice-activation registry ---
$backup = Join-Path (Split-Path $AarConfigPath -Parent) ("backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force $backup | Out-Null
if (Test-Path $AarConfigPath) { Copy-Item $AarConfigPath (Join-Path $backup "aarconfig.txt") }
reg.exe export $RegPath (Join-Path $backup "VoiceActivation.reg") /y | Out-Null
Write-Host "[*] Backup: $backup  (undo with -Restore '$backup')" -ForegroundColor Cyan

# --- 2. Rewrite aarconfig.txt: one owner per model GUID, one language for all apps ---
$lines = @()
if (Test-Path $AarConfigPath) {
    foreach ($line in Get-Content $AarConfigPath) {
        $f = $line.Split(',')
        if ($f.Length -ne 5) { continue }
        if ($f[1].Trim() -ieq $ModelId -or $f[3].Trim() -ieq $aumid) {
            Write-Host "    unbinding: $line" -ForegroundColor Yellow
            continue
        }
        if ([int]$f[2] -ne $Lcid) { throw "aarconfig line uses LCID $($f[2]); all apps must share LCID $Lcid ($line)" }
        $lines += $line
    }
}
$lines += "$Keyword,$ModelId,$Lcid,$aumid,background"
New-Item -ItemType Directory -Force (Split-Path $AarConfigPath -Parent) | Out-Null
[IO.File]::WriteAllLines($AarConfigPath, [string[]]$lines)
Write-Host "[*] $AarConfigPath now:" -ForegroundColor Cyan
$lines | ForEach-Object { "    $_" }

# --- 3. App language + Windows voice-activation permission ---
$appPath = Join-Path $Root $aumid
New-Item -Path $appPath -Force | Out-Null
New-ItemProperty -Path $appPath -Name "LANGID" -Value $Lcid -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $appPath -Name "IsLanguageAvailable" -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $appPath -Name "AgentActivationEnabled" -Value 1 -PropertyType DWord -Force | Out-Null

# --- 4. Drop stale bindings so AarSvc regenerates them from the file ---
$sdc = Join-Path $Root "SignalDetectionConfigurations"
if (Test-Path $sdc) {
    Get-ChildItem $sdc | ForEach-Object { Remove-Item $_.PSPath -Recurse -Force }
}
New-ItemProperty -Path $Root -Name "UseAarConfigFile" -Value 1 -PropertyType DWord -Force | Out-Null

# --- 5. Launch the host (registers its ConversationalAgentTrigger), then restart AarSvc to load the file ---
Start-Process "shell:AppsFolder\$aumid"
Start-Sleep -Seconds 5
Restart-Aar
Start-Sleep -Seconds 3
Write-Host "[*] Bindings rebuilt by AarSvc:" -ForegroundColor Cyan
if (Test-Path $sdc) {
    Get-ChildItem $sdc -Recurse | Where-Object { $_.Property -contains "Active" } | ForEach-Object {
        $p = Get-ItemProperty $_.PSPath
        "    {0}  Available={1} Active={2}" -f ($_.Name -replace '^.*SignalDetectionConfigurations\\', ''), $p.Available, $p.Active
    }
}
Write-Host "[OK] '$Keyword' bound to $aumid." -ForegroundColor Green
