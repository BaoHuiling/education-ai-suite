# =============================================================================
# start_wakeword.ps1
# AI Teaching Assistant - Wake-word launcher
#
# Reads ATA_WAKEWORD_BACKEND from .env and starts the selected wake-word
# backend:
#   software -> openWakeWord via mic_session.py (CPU, works on any machine)
#   hardware -> ATA's packaged Windows voice-activation agent
#
# The hardware agent must be installed and its AUMID configured in .env.
#
# Run start_ata.ps1 first so kiosk-core (port 8012) is up to receive the
# session trigger.
#
# Usage: powershell -ExecutionPolicy Bypass -File start_wakeword.ps1
# =============================================================================

param(
    [string]$WakewordModel = "hey jarvis",  # software (openWakeWord) model name
    [switch]$Silent = $false
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Info { param([string]$m) if (-not $Silent) { Write-Host "[*] $m" -ForegroundColor Yellow } }
function Write-Success { param([string]$m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-ErrorCustom { param([string]$m) Write-Host "[!] $m" -ForegroundColor Red }

# Load .env into the current process environment.
function Load-Env {
    $EnvFile = Join-Path $ScriptDir ".env"
    if (-not (Test-Path $EnvFile)) {
        Write-ErrorCustom ".env not found at $EnvFile. Run setup_windows.ps1 first."
        exit 1
    }
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]*)=(.*)$") {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
        }
    }
}

# Capability probe for the Intel DSP/driver stack.
function Test-IntelWakeHardwareAvailable {
    $aar = Get-Service -Name "AarSvc*" -ErrorAction SilentlyContinue
    $dsp = Get-PnpDevice -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -match "Smart Sound" -and $_.Status -eq "OK" }
    return [bool]($aar -and $dsp)
}

function Start-SoftwareWakeword {
    Write-Info "Starting software wake-word (openWakeWord, model '$WakewordModel')..."
    $mic = Join-Path $ScriptDir "mic_session.py"
    if (-not (Test-Path $mic)) {
        Write-ErrorCustom "mic_session.py not found at $mic"
        exit 1
    }
    python $mic --wakeword --wakeword-model $WakewordModel
}

function Start-HardwareWakeword {
    if (-not (Test-IntelWakeHardwareAvailable)) {
        Write-ErrorCustom "Intel DSP/AAR hardware was not detected."
        return $false
    }
    $agentAumid = $env:ATA_WAKEWORD_AGENT_AUMID
    if ([string]::IsNullOrWhiteSpace($agentAumid)) {
        Write-ErrorCustom "ATA_WAKEWORD_AGENT_AUMID is empty. Install wakeword-agent\package.ps1 -Install first."
        return $false
    }
    Write-Info "Arming ATA's own DSP wake agent ($agentAumid)..."
    try {
        Start-Process "shell:AppsFolder\$agentAumid"
        Write-Success "ATA wake agent launched. Windows will activate it when its provisioned keyword is detected."
        return $true
    }
    catch {
        Write-ErrorCustom "Could not launch ATA's wake agent: $_"
        return $false
    }
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
Load-Env

$backend = $env:ATA_WAKEWORD_BACKEND
if ([string]::IsNullOrWhiteSpace($backend)) { $backend = "software" }
$fallback = $env:ATA_WAKEWORD_FALLBACK
if ([string]::IsNullOrWhiteSpace($fallback)) { $fallback = "software" }

Write-Info "Configured wake-word backend: $backend"

if ($backend -eq "hardware") {
    if (Test-IntelWakeHardwareAvailable) {
        if (-not (Start-HardwareWakeword)) {
            if ($fallback -eq "software") {
                Write-Info "Falling back to software wake-word."
                Start-SoftwareWakeword
            }
            else {
                Write-ErrorCustom "Hardware launch failed and fallback is disabled."
                exit 1
            }
        }
    }
    else {
        Write-ErrorCustom "Hardware wake-word requested but no Intel wake-capable DSP was detected."
        if ($fallback -eq "software") {
            Write-Info "Falling back to software wake-word."
            Start-SoftwareWakeword
        }
        else {
            Write-ErrorCustom "Fallback is disabled (ATA_WAKEWORD_FALLBACK=$fallback). Nothing to start."
            exit 1
        }
    }
}
else {
    Start-SoftwareWakeword
}
