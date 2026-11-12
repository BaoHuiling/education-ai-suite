#!/usr/bin/env pwsh
param(
    [switch]$SkipProxy,
    [switch]$Restart,
    [switch]$Help,
    [switch]$NoElevate,
    [switch]$Silent,
    [switch]$NoWindowsTerminal,
    [switch]$Electron,
    # DEPRECATED(webui): browser UI mode, remove after 2027.0.0.
    [switch]$WebUI
)

# ============================================================================
# WINDOWS-ONLY CHECK
# ============================================================================
$IsWindowsOS = $IsWindows -or ($PSVersionTable.PSVersion.Major -lt 6) -or ($env:OS -eq "Windows_NT")

if (-not $IsWindowsOS) {
    Write-Host "ERROR: This script is designed for Windows only." -ForegroundColor Red
    exit 1
}

# Disable QuickEdit Mode on conhost to prevent the process from hanging
function Disable-ConsoleQuickEdit {
    try {
        if (-not ('SmartClassroom.ConsoleMode' -as [type])) {
            Add-Type -Namespace SmartClassroom -Name ConsoleMode -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
'@ -ErrorAction Stop
        }

        $handle = [SmartClassroom.ConsoleMode]::GetStdHandle(-10)  # STD_INPUT_HANDLE
        if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr](-1)) { return }

        $mode = [uint32]0
        if (-not [SmartClassroom.ConsoleMode]::GetConsoleMode($handle, [ref]$mode)) { return }

        # ENABLE_EXTENDED_FLAGS (0x0080) must be set for the console to honour
        # a cleared ENABLE_QUICK_EDIT_MODE (0x0040).
        $newMode = [uint32](($mode -band (-bnot 0x0040)) -bor 0x0080)
        if ($newMode -ne $mode) {
            [void][SmartClassroom.ConsoleMode]::SetConsoleMode($handle, $newMode)
        }
    } catch {
        # No real console attached (redirected output, ISE, ...)
    }
}

if (-not $env:WT_SESSION) { Disable-ConsoleQuickEdit }

if ($Help) {
    Write-Host @"
Smart Classroom Startup Script

Usage: ./start-smart-classroom.ps1 [-SkipProxy] [-Silent] [-Help]
       ./start-smart-classroom.ps1 -WebUI [-SkipProxy] [-Restart] [-Silent] [-NoElevate] [-NoWindowsTerminal]

By default this launches the desktop app (same as ./start-desktop-app.ps1),
which manages the Python services itself and never elevates.

Options:
    -SkipProxy           Skip proxy configuration prompts
    -Silent              Unattended mode - skip all prompts (with -WebUI: UAC included, auto-restart)
    -Electron            Accepted for compatibility; the desktop app is now the default
    -Help                Show this help message

DEPRECATED - browser UI, to be removed after 2027.0.0:
    -WebUI               Start the services in console windows and serve the UI
                         in a browser at http://localhost:5173
    -Restart             Kill existing services and restart (no prompt)
    -NoElevate           Never show a UAC prompt; enabling long paths is skipped if it needs one
    -NoWindowsTerminal   Launch through WMI instead of Windows Terminal (for remote sessions)

    With -WebUI the services run as the current user, never as Administrator.
    Only enabling Windows long paths needs Administrator; when it is off, a UAC
    prompt asks for that one change (not with -Silent or -NoElevate).

    Services launched with -WebUI (in order):
    1. Backend (port 8000)     - Main Python pipeline service; runs in this terminal, or a detached logged process with -NoWindowsTerminal (with paddleocr if OCR enabled)
    2. Content Search (9011)   - RAG, video summarization, semantic search
    3. Grading (9902 + 9012)   - Layout detection + VLM grading service (if grading.enabled)
    4. Frontend (port 5173)    - React UI in a browser tab, launched in a NEW terminal

"@ -ForegroundColor Cyan
    exit 0
}

# ============================================================================
# DESKTOP APP MODE (default)
# ============================================================================
# The Electron app supervises the Python services itself, so the default just
# delegates.
if ($WebUI -and $Electron) {
    Write-Host "ERROR: -WebUI and -Electron cannot be combined." -ForegroundColor Red
    exit 1
}

if (-not $WebUI) {
    # These only make sense when this script runs the services. Fail instead of
    # opening a desktop window that an unattended or remote caller cannot use.
    $webOnly = @()
    if ($Restart) { $webOnly += "-Restart" }
    if ($NoWindowsTerminal) { $webOnly += "-NoWindowsTerminal" }
    if ($webOnly) {
        Write-Host "ERROR: $($webOnly -join ', ') only applies to the deprecated browser UI." -ForegroundColor Red
        Write-Host "The desktop app is now the default. Add -WebUI to keep the old behavior" -ForegroundColor Yellow
        Write-Host "until it is removed after 2027.0.0, or run ./start-desktop-app.ps1." -ForegroundColor Yellow
        exit 1
    }

    $ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
    # The desktop app never elevates, so -NoElevate needs no forwarding.
    $desktopArgs = @{}
    if ($Silent -or $SkipProxy) { $desktopArgs['Silent'] = $true }
    & (Join-Path $ScriptDir "start-desktop-app.ps1") @desktopArgs
    exit $LASTEXITCODE
}

# ============================================================================
# DEPRECATED(webui): BROWSER UI MODE (-WebUI) - remove after 2027.0.0
# ============================================================================
# Everything below this banner runs only with -WebUI. When it is removed, this
# script reduces to the delegation above (or is dropped for start-desktop-app.ps1).
Write-Host ""
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "  DEPRECATED: the browser UI (-WebUI)" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "The browser UI and PowerShell-managed services will be removed after" -ForegroundColor Yellow
Write-Host "2027.0.0. Use the desktop app instead: ./start-desktop-app.ps1" -ForegroundColor Yellow
Write-Host "(or ./start-smart-classroom.ps1 without -WebUI)." -ForegroundColor Yellow
Write-Host ""

# ============================================================================
# PRIVILEGES
# ============================================================================
# The services run as the current user and never elevate, like the desktop app:
# what they create (exported models, caches, logs) must stay readable by a normal
# start. The one step that needs Administrator, enabling Windows long paths,
# asks for that change alone with its own UAC prompt (step 2), never with
# -Silent or -NoElevate.

# ============================================================================
# CTRL+C HANDLER - Stop services on script exit
# ============================================================================
$script:servicesStarted = $false

function Stop-AllServices {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "   STOPPING ALL SERVICES..." -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ""
    
    $ports = @(8000, 9011, 9902, 9012, 5173)
    $portNames = @{ 8000 = "Backend"; 9011 = "Content Search"; 9902 = "Layout Detection"; 9012 = "Grading"; 5173 = "Frontend" }
    
    foreach ($port in $ports) {
        Write-Host "  Stopping $($portNames[$port]) (port $port)..." -ForegroundColor Yellow
        
        # Retry killing processes on this port up to 3 times
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
                if ($connections) {
                    $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
                    foreach ($procId in $procIds) {
                        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                    }
                }
            } catch {
                # Continue to next method
            }
            
            Start-Sleep -Milliseconds 300
        }
        
        # Use taskkill as additional method
        try {
            taskkill /F /FI "LocalPort eq $port" 2>$null
        } catch {}
        
        Start-Sleep -Seconds 1
    }

    $connections = Get-NetTCPConnection -LocalPort 9090 -ErrorAction SilentlyContinue
    if ($connections) {
        Write-Host "  Stopping ChromaDB (port 9090)..." -ForegroundColor Yellow
        $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $procIds) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }

    $connections = Get-NetTCPConnection -LocalPort 9900 -ErrorAction SilentlyContinue
    if ($connections) {
        Write-Host "  Stopping VLM (port 9900)..." -ForegroundColor Yellow
        $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $procIds) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }

    $connections = Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue
    if ($connections) {
        Write-Host "  Stopping Preprocess (port 8001)..." -ForegroundColor Yellow
        $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $procIds) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }

    $connections = Get-NetTCPConnection -LocalPort 9990 -ErrorAction SilentlyContinue
    if ($connections) {
        Write-Host "  Stopping Ingest (port 9990)..." -ForegroundColor Yellow
        $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $procIds) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    
    # Comprehensive process cleanup (silent)
    
    # Kill all Python processes (multiple attempts)
    try {
        Get-Process python, python.exe -ErrorAction SilentlyContinue | ForEach-Object {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    
    # Kill all npm/node processes
    try {
        Get-Process node, npm -ErrorAction SilentlyContinue | ForEach-Object {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    
    # Kill uvicorn processes specifically
    try {
        Get-Process uvicorn -ErrorAction SilentlyContinue | ForEach-Object {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    
    # Final wait for ports to be freed
    Start-Sleep -Seconds 3
    
    # Verify ports are free
    $portsToVerify = @(8000, 9011, 9090, 9900, 8001, 9990, 5173)
    foreach ($port in $portsToVerify) {
        try {
            $connection = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
            if ($connection) {
                taskkill /F /PID $connection.OwningProcess 2>$null
                Start-Sleep -Seconds 1
            }
        } catch {}
    }
    
    Write-Host ""
    Write-Host "Services stopped.. Wait for the processes to get terminated...before start again..." -ForegroundColor Green
}

# Register Ctrl+C handler
if (-not $Silent) {
    [Console]::TreatControlCAsInput = $false
}
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    if ($script:servicesStarted) {
        Stop-AllServices
    }
}

trap {
    Write-Host ""
    Write-Host "  Script interrupted at line $($_.InvocationInfo.ScriptLineNumber) with $($_.Exception.Message)" -ForegroundColor Red
    if ($script:servicesStarted) {
        Stop-AllServices
        $script:servicesStarted = $false
    }
    exit 1
}

# ============================================================================
# PLATFORM DETECTION
# ============================================================================
$IsLinuxOS = $IsLinux -or ($PSVersionTable.Platform -eq "Unix")

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   SMART CLASSROOM STARTUP SCRIPT" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Platform: $(if ($IsWindowsOS) { 'Windows' } else { 'Linux' })" -ForegroundColor Yellow
Write-Host "PowerShell: $($PSVersionTable.PSVersion)" -ForegroundColor Yellow
if ($IsWindowsOS) {
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-Host "Admin: $(if ($isAdmin) { 'Yes' } else { 'No' })" -ForegroundColor $(if ($isAdmin) { 'Green' } else { 'Yellow' })
}
Write-Host ""

# ============================================================================
# SCRIPT DIRECTORY DETECTION
# ============================================================================
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if (-not $ScriptDir) {
    $ScriptDir = Get-Location
}

Write-Host "Working Directory: $ScriptDir" -ForegroundColor Gray
Set-Location $ScriptDir


$configPath = Join-Path $ScriptDir "config.yaml"
$contentSearchEnabled = $true
$videoSummarizationEnabled = $true
if (Test-Path $configPath) {
    $configContent = Get-Content $configPath -Raw
    $csFlag  = $configContent -match "content_search:\s*\{\s*enabled:\s*true"
    $segFlag = $configContent -match "topic_segmentation:\s*\{\s*enabled:\s*true"
    $qaFlag  = $configContent -match "qa:\s*\{\s*enabled:\s*true"
    $contentSearchEnabled = $csFlag -or $segFlag -or $qaFlag
    $videoSummarizationEnabled = -not ($configContent -match "video_summarization_enabled:\s*false")
}

# ============================================================================
# CHECK FOR RUNNING SERVICES
# ============================================================================
function Test-PortInUse {
    param([int]$Port)
    
    if ($IsWindowsOS) {
        $connection = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        return $null -ne $connection
    } else {
        $result = bash -c "ss -tuln | grep -q ':$Port '" 2>$null
        return $LASTEXITCODE -eq 0
    }
}

function Stop-ServiceOnPort {
    param([int]$Port, [string]$ServiceName)
    
    if ($IsWindowsOS) {
        $connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        if ($connections) {
            Write-Host "  Stopping $ServiceName on port $Port..." -ForegroundColor Yellow
            # PID 0/4 own TIME_WAIT and kernel connections; nothing to stop.
            $procIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 4 }
            foreach ($procId in $procIds) {
                try {
                    Stop-Process -Id $procId -Force -ErrorAction Stop
                    Write-Host "    Killed process $procId" -ForegroundColor Gray
                } catch {
                    $win32 = $_.Exception.InnerException
                    if ($win32 -is [System.ComponentModel.Win32Exception] -and $win32.NativeErrorCode -eq 5) {
                        # Typically left by an earlier start that ran as Administrator.
                        Write-Host "    Could not stop process $procId (access denied): it runs as Administrator." -ForegroundColor Yellow
                        Write-Host "    Close the window that started it, or stop it from an Administrator prompt." -ForegroundColor Yellow
                    } elseif (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                        Write-Host "    Could not stop process ${procId}: $($_.Exception.Message)" -ForegroundColor Yellow
                    }
                }
            }
            Start-Sleep -Seconds 2
        }
    } else {
        $result = bash -c "ss -tuln | grep -q ':$Port '" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  Stopping $ServiceName on port $Port..." -ForegroundColor Yellow
            bash -c "fuser -k $Port/tcp" 2>$null
            Start-Sleep -Seconds 2
        }
    }
}

# Function to clean up virtual environments for fresh restart
function Remove-VirtualEnvironments {
    Write-Host "  Cleaning up virtual environments..." -ForegroundColor Yellow
    
    $parentDir = Split-Path $ScriptDir -Parent
    $backendVenv = Join-Path $parentDir "smartclassroom"

    Write-Host "    Terminating Python processes that may be using venvs..." -ForegroundColor Gray
    Get-Process -Name "python" -ErrorAction SilentlyContinue | ForEach-Object {
        $procPath = $_.Path
        if ($procPath -and $procPath -like "*smartclassroom*") {
            Write-Host "      Killing Python process $($_.Id): $procPath" -ForegroundColor Gray
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
    
    if (Test-Path $backendVenv) {
        Write-Host "    Removing Backend venv: $backendVenv" -ForegroundColor Gray
        for ($i = 1; $i -le 3; $i++) {
            Remove-Item -Path $backendVenv -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $backendVenv)) { break }
            Write-Host "      Retry $i - waiting for file handles to release..." -ForegroundColor DarkYellow
            Start-Sleep -Seconds 2
        }
        if (Test-Path $backendVenv) {
            Write-Host "    WARNING: Could not fully remove Backend venv. Some files may be locked." -ForegroundColor Yellow
        } else {
            Write-Host "    Backend venv removed." -ForegroundColor Gray
        }
    } else {
        Write-Host "    Backend venv not found (will be created fresh)" -ForegroundColor Gray
    }
    
    Write-Host "  Virtual environments cleaned." -ForegroundColor Green
}

Write-Host ""
Write-Host "[PRE-CHECK] DETECTING RUNNING SERVICES" -ForegroundColor Cyan
Write-Host "--------------------------------------" -ForegroundColor Cyan

# Wait a moment for ports to be fully released from previous session
Write-Host "  Checking port status..." -ForegroundColor Gray
Start-Sleep -Seconds 2

# Enhanced port checking that verifies services are actually listening (not just in TIME_WAIT)
function Test-ServiceListening {
    param([int]$Port)
    
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State "Listen" -ErrorAction SilentlyContinue
        return $null -ne $connection
    } catch {
        return $false
    }
}

$backendRunning = Test-ServiceListening -Port 8000
$contentSearchRunning = Test-ServiceListening -Port 9011
$layoutDetectionRunning = Test-ServiceListening -Port 9902
$gradingRunning = Test-ServiceListening -Port 9012
$frontendRunning = Test-ServiceListening -Port 5173

$anyRunning = $backendRunning -or $contentSearchRunning -or $layoutDetectionRunning -or $gradingRunning -or $frontendRunning

$script:skipBackend = $backendRunning
$script:skipContentSearch = $contentSearchRunning
$script:skipFrontend = $frontendRunning

$gradingEnabled = $false
$configPath = Join-Path $ScriptDir "config.yaml"
if (Test-Path $configPath) {
    $configContent = Get-Content $configPath -Raw
    if ($configContent -match "grading:\s*\{[^}]*enabled:\s*(true|false)") {
        $gradingEnabled = $Matches[1] -eq "true"
    }
}

Write-Host ""
Write-Host "  Service Status:" -ForegroundColor Yellow
if ($backendRunning) {
    Write-Host "    [RUNNING] Backend (port 8000)" -ForegroundColor Green
} else {
    Write-Host "    [STOPPED] Backend (port 8000)" -ForegroundColor Red
}
if ($contentSearchRunning) { 
    Write-Host "    [RUNNING] Content Search (port 9011)" -ForegroundColor Green 
} elseif (-not $contentSearchEnabled) {
    Write-Host "    [DISABLED] Content Search (disabled in config)" -ForegroundColor DarkGray
} else { 
    Write-Host "    [STOPPED] Content Search (port 9011)" -ForegroundColor Red 
}
if ($gradingEnabled) {
    if ($layoutDetectionRunning) {
        Write-Host "    [RUNNING] Layout Detection (port 9902)" -ForegroundColor Green
    } else {
        Write-Host "    [STOPPED] Layout Detection (port 9902)" -ForegroundColor Red
    }
    if ($gradingRunning) {
        Write-Host "    [RUNNING] Grading (port 9012)" -ForegroundColor Green
    } else {
        Write-Host "    [STOPPED] Grading (port 9012)" -ForegroundColor Red
    }
}
if ($frontendRunning) {
    Write-Host "    [RUNNING] Frontend (port 5173)" -ForegroundColor Green
} else {
    Write-Host "    [STOPPED] Frontend (port 5173)" -ForegroundColor Red
}
Write-Host ""

if ($Restart) {
    # -Restart flag: stop all running services and start fresh
    Write-Host "  -Restart flag specified. Stopping all running services..." -ForegroundColor Yellow
    if ($backendRunning) { Stop-ServiceOnPort -Port 8000 -ServiceName "Backend" }
    if ($contentSearchRunning) {
        Stop-ServiceOnPort -Port 9011 -ServiceName "Content Search"
    }
    if ($layoutDetectionRunning) { Stop-ServiceOnPort -Port 9902 -ServiceName "Layout Detection" }
    if ($gradingRunning) { Stop-ServiceOnPort -Port 9012 -ServiceName "Grading" }
    Stop-ServiceOnPort -Port 9090 -ServiceName "ChromaDB"
    Stop-ServiceOnPort -Port 9900 -ServiceName "VLM"
    Stop-ServiceOnPort -Port 8001 -ServiceName "Preprocess"
    Stop-ServiceOnPort -Port 9990 -ServiceName "Ingest"
    if ($frontendRunning) { Stop-ServiceOnPort -Port 5173 -ServiceName "Frontend" }

    if ($Silent) {
        Write-Host "  Silent mode: keeping existing virtual environments" -ForegroundColor Gray
        $deleteVenvs = "N"
    } else {
        $deleteVenvs = Read-Host "  Delete virtual environments and create new? (Y/N)"
    }

    if ($deleteVenvs.ToUpper() -eq "Y") {
        Remove-VirtualEnvironments
        Write-Host "  All services will be restarted with new environments." -ForegroundColor Green
    } else {
        Write-Host "  Keeping existing virtual environments. Restarting services." -ForegroundColor Green
    }

    $script:skipBackend = $false
    $script:skipContentSearch = $false
    $script:skipFrontend = $false
} elseif ($anyRunning) {
    if ($Silent) {
        Write-Host "  Silent mode: auto-restarting all running services..." -ForegroundColor Yellow
        $choice = "R"
    } else {
        Write-Host "  What would you like to do?" -ForegroundColor Yellow
        Write-Host "    [R] Restart - Kill services and restart" -ForegroundColor White
        Write-Host "    [S] Skip    - Use existing services (only start missing ones)" -ForegroundColor White
        Write-Host "    [A] Abort   - Stop all services and exit" -ForegroundColor White
        Write-Host "    [E] Exit    - Exit script without changes" -ForegroundColor White
        Write-Host ""

        $choice = Read-Host "  Enter choice (R/S/A/E)"
    }

    switch ($choice.ToUpper()) {
        "R" {
            Write-Host ""
            Write-Host "  Restarting all services..." -ForegroundColor Yellow
            if ($backendRunning) { Stop-ServiceOnPort -Port 8000 -ServiceName "Backend" }
            if ($contentSearchRunning) {
                Stop-ServiceOnPort -Port 9011 -ServiceName "Content Search"
            }
            if ($layoutDetectionRunning) { Stop-ServiceOnPort -Port 9902 -ServiceName "Layout Detection" }
            if ($gradingRunning) { Stop-ServiceOnPort -Port 9012 -ServiceName "Grading" }
            Stop-ServiceOnPort -Port 9090 -ServiceName "ChromaDB"
            Stop-ServiceOnPort -Port 9900 -ServiceName "VLM"
            Stop-ServiceOnPort -Port 8001 -ServiceName "Preprocess"
            Stop-ServiceOnPort -Port 9990 -ServiceName "Ingest"
            if ($frontendRunning) { Stop-ServiceOnPort -Port 5173 -ServiceName "Frontend" }

            if ($Silent) {
                Write-Host "  Silent mode: keeping existing virtual environments" -ForegroundColor Gray
                $deleteVenvs = "N"
            } else {
                $deleteVenvs = Read-Host "  Delete virtual environments and create new? (Y/N)"
            }

            if ($deleteVenvs.ToUpper() -eq "Y") {
                Remove-VirtualEnvironments
            } else {
                Write-Host "  Keeping existing virtual environments." -ForegroundColor Gray
            }
            
            $script:skipBackend = $false
            $script:skipContentSearch = $false
            $script:skipFrontend = $false
            Write-Host "  Existing services stopped." -ForegroundColor Green
        }
        "S" {
            Write-Host ""
            Write-Host "  Smart Start: Keeping running services, starting stopped ones." -ForegroundColor Yellow
            $script:skipBackend = $backendRunning
            $script:skipContentSearch = $contentSearchRunning
            $script:skipFrontend = $frontendRunning
        }
        "A" {
            Write-Host ""
            Write-Host "  Stopping all services..." -ForegroundColor Yellow
            if ($backendRunning) { Stop-ServiceOnPort -Port 8000 -ServiceName "Backend" }
            if ($contentSearchRunning) {
                Stop-ServiceOnPort -Port 9011 -ServiceName "Content Search"
            }
            if ($layoutDetectionRunning) { Stop-ServiceOnPort -Port 9902 -ServiceName "Layout Detection" }
            if ($gradingRunning) { Stop-ServiceOnPort -Port 9012 -ServiceName "Grading" }
            Stop-ServiceOnPort -Port 9090 -ServiceName "ChromaDB"
            Stop-ServiceOnPort -Port 9900 -ServiceName "VLM"
            Stop-ServiceOnPort -Port 8001 -ServiceName "Preprocess"
            Stop-ServiceOnPort -Port 9990 -ServiceName "Ingest"
            if ($frontendRunning) { Stop-ServiceOnPort -Port 5173 -ServiceName "Frontend" }
            Write-Host "  All services stopped. Waiting for processes to terminate...Before starting new services..." -ForegroundColor Green
            exit 0
        }
        "E" {
            Write-Host ""
            Write-Host "  Exiting without changes. Services still running." -ForegroundColor Yellow
            exit 0
        }
        default {
            Write-Host ""
            Write-Host "  Invalid choice. Aborting." -ForegroundColor Red
            exit 1
        }
    }
} else {
    Write-Host "  No main services detected." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Stopping any orphaned processes (ChromaDB, VLM, Preprocess, Ingest, Python)..." -ForegroundColor Yellow
    
    Stop-ServiceOnPort -Port 9090 -ServiceName "ChromaDB"
    Stop-ServiceOnPort -Port 9900 -ServiceName "VLM"
    Stop-ServiceOnPort -Port 8001 -ServiceName "Preprocess"
    Stop-ServiceOnPort -Port 9990 -ServiceName "Ingest"
    
    Get-Process -Name "python" -ErrorAction SilentlyContinue | ForEach-Object {
        $procPath = $_.Path
        if ($procPath -and $procPath -like "*smartclassroom*") {
            Write-Host "    Killing orphaned Python: $($_.Id)" -ForegroundColor Gray
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    
    Write-Host ""
    Write-Host "  Starting all services..." -ForegroundColor Green
}

# Summary
Write-Host ""
Write-Host "  Action Summary:" -ForegroundColor Cyan
if ($script:skipBackend) { Write-Host "    Backend:        SKIP (already running)" -ForegroundColor Gray }
else { Write-Host "    Backend:        START" -ForegroundColor Green }
if (-not $contentSearchEnabled) { Write-Host "    Content Search: SKIP (disabled in config)" -ForegroundColor Gray }
elseif ($script:skipContentSearch) { Write-Host "    Content Search: SKIP (already running)" -ForegroundColor Gray }
else { Write-Host "    Content Search: START" -ForegroundColor Green }
if ($script:skipFrontend) { Write-Host "    Frontend:       SKIP (already running)" -ForegroundColor Gray }
else { Write-Host "    Frontend:       START" -ForegroundColor Green }

# ============================================================================
# STEP 1: PROXY CONFIGURATION (Persistent)
# ============================================================================
Write-Host ""
Write-Host "[1/4] PROXY CONFIGURATION" -ForegroundColor Green
Write-Host "-------------------------" -ForegroundColor Green

$httpProxy = ""
$httpsProxy = ""
$noProxy = ""
$proxyConfigFile = Join-Path $ScriptDir ".proxy-config"

# Python's urllib/requests/httpx split no_proxy on COMMAS only. A Windows-style
# semicolon list - which is what .proxy-config usually holds - is then read as
# one bogus host, so every 127.0.0.1 call in the services goes out to the
# corporate proxy and comes back 403. Normalise the separator and always keep
# the loopback entries, so local service-to-service calls stay local.
function Format-NoProxy {
    param([string]$Value)

    $entries = @()
    if ($Value) {
        $entries = $Value -split '[;,]' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }
    foreach ($loopback in @('localhost', '127.0.0.1', '::1')) {
        if ($entries -notcontains $loopback) { $entries += $loopback }
    }
    return ($entries -join ',')
}

if (-not $SkipProxy -and -not $Silent) {
    if (Test-Path $proxyConfigFile) {
        $proxyConfig = Get-Content $proxyConfigFile | ConvertFrom-Json
        $httpProxy = $proxyConfig.httpProxy
        $httpsProxy = $proxyConfig.httpsProxy
        $noProxy = $proxyConfig.noProxy

        Write-Host ""
        Write-Host "  Saved proxy settings found:" -ForegroundColor Cyan
        if ($httpProxy) { Write-Host "    HTTP_PROXY:  $httpProxy" -ForegroundColor Gray }
        if ($httpsProxy) { Write-Host "    HTTPS_PROXY: $httpsProxy" -ForegroundColor Gray }
        if ($noProxy) { Write-Host "    NO_PROXY:    $noProxy" -ForegroundColor Gray }
        if (-not $httpProxy -and -not $httpsProxy) { 
            Write-Host "    (No proxy configured in .proxy-config)" -ForegroundColor Gray 
            
            # Check environment for proxy settings
            Write-Host ""
            Write-Host "  Checking environment for existing proxy settings..." -ForegroundColor Gray
            $envHttpProxy = if ($env:HTTP_PROXY) { $env:HTTP_PROXY } elseif ($env:http_proxy) { $env:http_proxy } else { "" }
            $envHttpsProxy = if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } elseif ($env:https_proxy) { $env:https_proxy } else { "" }
            $envNoProxy = if ($env:NO_PROXY) { $env:NO_PROXY } elseif ($env:no_proxy) { $env:no_proxy } else { "" }
            
            $envProxies = Get-ChildItem Env:\*proxy* -ErrorAction SilentlyContinue
            if ($envProxies) {
                $envProxies | ForEach-Object {
                    Write-Host "    Found: $($_.Name) = $($_.Value)" -ForegroundColor Cyan
                }
                Write-Host ""
                Write-Host "  Environment variables detected. You can save these to .proxy-config." -ForegroundColor Yellow
            } else {
                Write-Host "    (No proxy environment variables found)" -ForegroundColor Gray
            }
        }
        Write-Host ""

        if (-not $httpProxy -and -not $httpsProxy -and ($envHttpProxy -or $envHttpsProxy)) {
            Write-Host "  [Y] Yes - Configure different proxy settings" -ForegroundColor White
            Write-Host "  [N] No  - Save environment proxy settings to .proxy-config" -ForegroundColor White
            Write-Host "  [S] Skip - No proxy (direct connection)" -ForegroundColor White
        } else {
            Write-Host "  [Y] Yes - Change proxy settings" -ForegroundColor White
            Write-Host "  [N] No  - Use saved proxy settings" -ForegroundColor White
            Write-Host "  [S] Skip - No proxy (direct connection)" -ForegroundColor White
        }
        Write-Host ""
        $changeProxy = Read-Host "Do you want to change proxy settings? (Y/N/S)"
        
        if ($changeProxy -match "^[Yy]") {
            Write-Host ""
            Write-Host "Enter new proxy settings (press Enter to keep current value):" -ForegroundColor Yellow
            Write-Host ""
            
            $newHttpProxy = Read-Host "HTTP_PROXY  [$httpProxy]"
            $newHttpsProxy = Read-Host "HTTPS_PROXY [$httpsProxy]"
            $newNoProxy = Read-Host "NO_PROXY    [$noProxy]"
            
            if ($newHttpProxy) { $httpProxy = $newHttpProxy }
            if ($newHttpsProxy) { $httpsProxy = $newHttpsProxy }
            if ($newNoProxy) { $noProxy = $newNoProxy }
            
            $proxyConfig = @{
                httpProxy = $httpProxy
                httpsProxy = $httpsProxy
                noProxy = $noProxy
            }
            $proxyConfig | ConvertTo-Json | Set-Content $proxyConfigFile
            Write-Host "  Proxy settings updated and saved." -ForegroundColor Green
        } elseif ($changeProxy -match "^[Ss]") {
            $httpProxy = ""
            $httpsProxy = ""
            $noProxy = ""
            Write-Host "  No proxy - using direct connection." -ForegroundColor Yellow
        } else {
            # If .proxy-config is empty but environment has proxy, save environment values
            if (-not $httpProxy -and -not $httpsProxy -and ($envHttpProxy -or $envHttpsProxy)) {
                $httpProxy = $envHttpProxy
                $httpsProxy = $envHttpsProxy
                $noProxy = $envNoProxy
                
                $proxyConfig = @{
                    httpProxy = $httpProxy
                    httpsProxy = $httpsProxy
                    noProxy = $noProxy
                }
                $proxyConfig | ConvertTo-Json | Set-Content $proxyConfigFile
                Write-Host "  Environment proxy settings saved to .proxy-config:" -ForegroundColor Green
                if ($httpProxy) { Write-Host "    HTTP_PROXY:  $httpProxy" -ForegroundColor Gray }
                if ($httpsProxy) { Write-Host "    HTTPS_PROXY: $httpsProxy" -ForegroundColor Gray }
                if ($noProxy) { Write-Host "    NO_PROXY:    $noProxy" -ForegroundColor Gray }
            } else {
                Write-Host "  Using saved proxy settings." -ForegroundColor Gray
            }
        }
    } else {
        Write-Host ""
        Write-Host "  No proxy configuration found in .proxy-config file." -ForegroundColor Gray
        Write-Host "  Checking environment for existing proxy settings..." -ForegroundColor Gray
        Write-Host ""
        
        # Check environment variables for proxy settings
        $envHttpProxy = if ($env:HTTP_PROXY) { $env:HTTP_PROXY } elseif ($env:http_proxy) { $env:http_proxy } else { "" }
        $envHttpsProxy = if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } elseif ($env:https_proxy) { $env:https_proxy } else { "" }
        $envNoProxy = if ($env:NO_PROXY) { $env:NO_PROXY } elseif ($env:no_proxy) { $env:no_proxy } else { "" }
        
        $envProxies = Get-ChildItem Env:\*proxy* -ErrorAction SilentlyContinue
        if ($envProxies) {
            $envProxies | ForEach-Object {
                Write-Host "    Found: $($_.Name) = $($_.Value)" -ForegroundColor Cyan
            }
            Write-Host ""
            Write-Host "  Environment variables detected. You can save these or configure different settings." -ForegroundColor Yellow
        } else {
            Write-Host "    (No proxy environment variables found)" -ForegroundColor Gray
        }
        Write-Host ""
        
        if ($envHttpProxy -or $envHttpsProxy) {
            Write-Host "  [Y] Yes - Configure different proxy settings" -ForegroundColor White
            Write-Host "  [N] No  - Save current environment proxy settings to .proxy-config" -ForegroundColor White
        } else {
            Write-Host "  [Y] Yes - Configure proxy" -ForegroundColor White
            Write-Host "  [N] No  - No proxy (direct connection)" -ForegroundColor White
        }
        Write-Host ""
        $configureProxy = Read-Host "Do you want to configure a proxy? (Y/N)"
        
        if ($configureProxy -match "^[Yy]") {
            Write-Host ""
            Write-Host "Enter proxy settings:" -ForegroundColor Yellow
            Write-Host "  (Common Intel proxy: http://proxy-iind.intel.com:912)" -ForegroundColor DarkGray
            Write-Host ""
            
            $httpProxy = Read-Host "HTTP_PROXY"
            $httpsProxy = Read-Host "HTTPS_PROXY (press Enter to use same as HTTP)"
            $noProxy = Read-Host "NO_PROXY"
            
            if (-not $httpsProxy -and $httpProxy) { $httpsProxy = $httpProxy }
            
            $proxyConfig = @{
                httpProxy = $httpProxy
                httpsProxy = $httpsProxy
                noProxy = $noProxy
            }
            $proxyConfig | ConvertTo-Json | Set-Content $proxyConfigFile
            Write-Host "  Proxy settings saved to .proxy-config" -ForegroundColor Green
        } else {
            # If environment variables exist, save them; otherwise save empty config
            if ($envHttpProxy -or $envHttpsProxy) {
                $httpProxy = $envHttpProxy
                $httpsProxy = $envHttpsProxy
                $noProxy = $envNoProxy
                
                $proxyConfig = @{
                    httpProxy = $httpProxy
                    httpsProxy = $httpsProxy
                    noProxy = $noProxy
                }
                $proxyConfig | ConvertTo-Json | Set-Content $proxyConfigFile
                Write-Host "  Environment proxy settings saved to .proxy-config:" -ForegroundColor Green
                if ($httpProxy) { Write-Host "    HTTP_PROXY:  $httpProxy" -ForegroundColor Gray }
                if ($httpsProxy) { Write-Host "    HTTPS_PROXY: $httpsProxy" -ForegroundColor Gray }
                if ($noProxy) { Write-Host "    NO_PROXY:    $noProxy" -ForegroundColor Gray }
            } else {
                $proxyConfig = @{
                    httpProxy = ""
                    httpsProxy = ""
                    noProxy = ""
                }
                $proxyConfig | ConvertTo-Json | Set-Content $proxyConfigFile
                Write-Host "  No proxy configured. Settings saved." -ForegroundColor Gray
            }
        }
    }
    
    if ($httpProxy) {
        $env:HTTP_PROXY = $httpProxy
        $env:http_proxy = $httpProxy
        Write-Host "  Applied HTTP_PROXY=$httpProxy" -ForegroundColor Gray
    }
    
    if ($httpsProxy) {
        $env:HTTPS_PROXY = $httpsProxy
        $env:https_proxy = $httpsProxy
        Write-Host "  Applied HTTPS_PROXY=$httpsProxy" -ForegroundColor Gray
    }
    
    # Always set it, even with no saved value: the loopback entries matter
    # whenever a proxy is configured.
    $noProxy = Format-NoProxy $noProxy
    $env:NO_PROXY = $noProxy
    $env:no_proxy = $noProxy
    Write-Host "  Applied NO_PROXY=$noProxy" -ForegroundColor Gray
} else {
    # -SkipProxy flag: load saved settings without prompting user
    Write-Host "  Loading proxy from .proxy-config (skipping prompts)..." -ForegroundColor Gray
    
    if (Test-Path $proxyConfigFile) {
        $proxyConfig = Get-Content $proxyConfigFile | ConvertFrom-Json
        $httpProxy = $proxyConfig.httpProxy
        $httpsProxy = $proxyConfig.httpsProxy
        $noProxy = $proxyConfig.noProxy
        
        if ($httpProxy) {
            $env:HTTP_PROXY = $httpProxy
            $env:http_proxy = $httpProxy
            Write-Host "  Applied HTTP_PROXY=$httpProxy" -ForegroundColor Gray
        }
        
        if ($httpsProxy) {
            $env:HTTPS_PROXY = $httpsProxy
            $env:https_proxy = $httpsProxy
            Write-Host "  Applied HTTPS_PROXY=$httpsProxy" -ForegroundColor Gray
        }
        
        $noProxy = Format-NoProxy $noProxy
        $env:NO_PROXY = $noProxy
        $env:no_proxy = $noProxy
        Write-Host "  Applied NO_PROXY=$noProxy" -ForegroundColor Gray

        if (-not $httpProxy -and -not $httpsProxy) {
            Write-Host "  Checking environment for existing proxy settings..." -ForegroundColor Gray
            Get-ChildItem Env:\*proxy* -ErrorAction SilentlyContinue | ForEach-Object {
                Write-Host "    Found: $($_.Name) = $($_.Value)" -ForegroundColor DarkGray
            }
            Write-Host "  No proxy configured in .proxy-config" -ForegroundColor Gray
        }
    } else {
        Write-Host "  Checking environment for existing proxy settings..." -ForegroundColor Gray
        Get-ChildItem Env:\*proxy* -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "    Found: $($_.Name) = $($_.Value)" -ForegroundColor DarkGray
        }
        Write-Host "  No .proxy-config file found" -ForegroundColor Gray

        # An inherited proxy still applies here, so keep loopback out of it.
        $noProxy = Format-NoProxy $env:NO_PROXY
        $env:NO_PROXY = $noProxy
        $env:no_proxy = $noProxy
        Write-Host "  Applied NO_PROXY=$noProxy" -ForegroundColor Gray
    }
}

# ============================================================================
# STEP 2: WINDOWS LONG PATHS & EXECUTION POLICY
# ============================================================================
Write-Host ""
Write-Host "[2/4] SYSTEM CONFIGURATION" -ForegroundColor Green
Write-Host "--------------------------" -ForegroundColor Green

if ($IsWindowsOS) {
    Write-Host "  Checking Windows Long Paths..." -ForegroundColor Gray

    # Reading the key needs no privileges; only setting it does. As in the
    # desktop app's Setup screen, Administrator is requested for this one
    # change, never for the services.
    $regPath = "HKLM:\System\CurrentControlSet\Control\FileSystem"
    $longPathsEnabled = { (Get-ItemProperty -Path $regPath -Name "LongPathsEnabled" -ErrorAction SilentlyContinue).LongPathsEnabled -eq 1 }

    if (& $longPathsEnabled) {
        Write-Host "  Long paths already enabled" -ForegroundColor Gray
    } elseif ($isAdmin) {
        try {
            New-ItemProperty -Path $regPath -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force | Out-Null
            Write-Host "  Long paths enabled successfully" -ForegroundColor Green
        } catch {
            Write-Host "  Warning: Could not modify registry - $($_.Exception.Message)" -ForegroundColor Yellow
        }
    } elseif ($Silent -or $NoElevate) {
        Write-Host "  Long paths are disabled; enabling them needs Administrator, which -Silent/-NoElevate never requests." -ForegroundColor Yellow
        Write-Host "  Model downloads with deep paths can fail. Enable them once from an Administrator prompt." -ForegroundColor Yellow
    } else {
        Write-Host "  Requesting Administrator privileges to enable long paths..." -ForegroundColor Yellow
        try {
            Start-Process -FilePath "reg.exe" -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList @(
                'add', 'HKLM\System\CurrentControlSet\Control\FileSystem',
                '/v', 'LongPathsEnabled', '/t', 'REG_DWORD', '/d', '1', '/f')
        } catch {
            # Declined UAC prompt: reported by the check below.
        }
        if (& $longPathsEnabled) {
            Write-Host "  Long paths enabled successfully" -ForegroundColor Green
        } else {
            Write-Host "  Long paths are still disabled (the prompt may have been declined); model downloads with deep paths can fail." -ForegroundColor Yellow
        }
    }

    Write-Host "  Setting execution policy to Bypass..." -ForegroundColor Gray
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force -ErrorAction SilentlyContinue
    Write-Host "  Execution policy set" -ForegroundColor Gray
    
} else {
    Write-Host "  Linux detected - skipping Windows-specific configuration" -ForegroundColor Gray
}

# ============================================================================
# STEP 3: CHECK OCR CONFIG
# ============================================================================
Write-Host ""
Write-Host "[3/4] CHECKING CONFIGURATION" -ForegroundColor Green
Write-Host "----------------------------" -ForegroundColor Green

# $configPath and $contentSearchEnabled were computed earlier (near script start).
if (Test-Path $configPath) {
    if ($configContent -match "ocr_enabled:\s*true") {
        Write-Host "  Document OCR: Enabled" -ForegroundColor Yellow
    } else {
        Write-Host "  Document OCR: Disabled" -ForegroundColor Gray
    }

    if ($configContent -match "board_ocr\s*:\s*\{\s*enabled\s*:\s*true\s*\}") {
        Write-Host "  Board OCR: Enabled" -ForegroundColor Yellow
    } else {
        Write-Host "  Board OCR: Disabled" -ForegroundColor Gray
    }

    if ($contentSearchEnabled) {
        Write-Host "  Content Search: Enabled (content_search/topic_segmentation/qa)" -ForegroundColor Yellow
    } else {
        Write-Host "  Content Search: Disabled" -ForegroundColor Gray
    }
} else {
    Write-Host "  config.yaml not found, assuming OCR disabled" -ForegroundColor Gray
    Write-Host "  config.yaml not found, assuming Content Search enabled" -ForegroundColor Gray
}

# Check Node.js
$npmExists = Get-Command npm -ErrorAction SilentlyContinue
if ($npmExists) {
    Write-Host "  Node.js/npm: Found ($(npm --version))" -ForegroundColor Green
} else {
    Write-Host "  Node.js/npm: Not found - Frontend will fail!" -ForegroundColor Red
}

# ============================================================================
# STEP 4: LAUNCH SERVICES
# ============================================================================
Write-Host ""
Write-Host "[4/4] LAUNCHING SERVICES" -ForegroundColor Green
Write-Host "------------------------" -ForegroundColor Green
Write-Host ""
Write-Host "Services will start with health checks:" -ForegroundColor Yellow
if ($NoWindowsTerminal) {
    Write-Host "  1. Backend (port 8000) - detached hidden process (output logged to file), wait until healthy" -ForegroundColor White
} else {
    Write-Host "  1. Backend (port 8000) - runs in THIS terminal, wait until healthy" -ForegroundColor White
}
Write-Host "  2. Content Search (port 9011) - wait until healthy, including the" -ForegroundColor White
Write-Host "     services its launcher spawns: video preprocess (8001), file ingest (9990), ChromaDB (9090)" -ForegroundColor White
Write-Host "  3. Frontend (port 5173) - launches in a NEW terminal" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop all services and exit." -ForegroundColor DarkGray
Write-Host ""

# Mark that services are being started (for Ctrl+C handler)
$script:servicesStarted = $true

function Get-HealthDetail {
    param($ErrorRecord)

    $body = $null
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
        $body = $ErrorRecord.ErrorDetails.Message          # PowerShell 7
    } elseif ($ErrorRecord.Exception.Response) {
        try {                                              # Windows PowerShell 5.1
            $stream = $ErrorRecord.Exception.Response.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            $reader.Close()
        } catch {}
    }
    if (-not $body) { return "" }

    try { $json = $body | ConvertFrom-Json } catch { return "" }
    if (-not $json.services) { return "" }

    $pending = @()
    foreach ($svc in $json.services.PSObject.Properties) {
        if ($svc.Value -ne "healthy") { $pending += "$($svc.Name)=$($svc.Value)" }
    }
    if ($pending.Count -eq 0) { return "" }
    return " - pending: $($pending -join ', ')"
}

# Prints the tail of a service's log files on failure, so a detached (windowless)
# backend's output is still diagnosable.
function Show-ServiceLogTail {
    param([string[]]$LogPaths = @(), [int]$Lines = 40)
    foreach ($p in $LogPaths) {
        if ($p -and (Test-Path $p)) {
            $tail = Get-Content -Path $p -Tail $Lines -ErrorAction SilentlyContinue
            if ($tail) {
                Write-Host "  --- last $Lines lines of $p ---" -ForegroundColor DarkYellow
                $tail | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
                Write-Host ""
            }
        }
    }
}

function Start-DetachedBackend {
    param(
        [string]$BackendScript,
        [string]$LogPath,
        [string]$ErrorPath
    )

    $logLiteral = $LogPath.Replace("'", "''")
    $errorLiteral = $ErrorPath.Replace("'", "''")
    $loggedScript = "& {`r`n$BackendScript`r`n} 6>&1 1> '$logLiteral' 2> '$errorLiteral'"
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($loggedScript))
    $powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

    # WMI creates the launcher outside the SSH session's process tree. Hidden
    # Start-Process children can still be terminated when that session closes.
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 } -ErrorAction Stop
    $launch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = "`"$powershellPath`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded"
        ProcessStartupInformation = $startup
    } -ErrorAction Stop
    if ($launch.ReturnValue -ne 0 -or -not $launch.ProcessId) {
        throw "Failed to start Backend through WMI (code $($launch.ReturnValue))."
    }
    return [uint32]$launch.ProcessId
}

function Test-ServiceLauncherRunning {
    param(
        [System.Diagnostics.Process]$Process = $null,
        [uint32]$LauncherProcessId = 0
    )

    if ($LauncherProcessId) {
        # WMI launchers have no reliable .NET exit-code handle. Query their PID,
        # and let query failures surface rather than misreporting a crash.
        return $null -ne (Get-CimInstance Win32_Process -Filter "ProcessId=$LauncherProcessId" -ErrorAction Stop)
    }
    return $Process -and -not $Process.HasExited
}

# Health check function (no timeout - relies on crash detection)
function Wait-ForService {
    param(
        [string]$ServiceName,
        [string]$Url,
        [int]$Port,
        [int[]]$DependentPorts = @(),
        [string]$CommandLinePattern = "",  # Pattern to match in process command line (e.g., "main.py", "start_services.py")
        [System.Diagnostics.Process]$Process = $null,  # Launched process to watch for early exit
        [uint32]$LauncherProcessId = 0,  # WMI-launched wrapper to watch by PID
        [string[]]$LogPaths = @(),  # Log files to tail when the service fails to start
        [int]$IntervalSeconds = 5,
        # Ceiling for aggregate endpoints, where the port stays open (so the
        # crash detection below never fires) while a service behind it is dead.
        # 0 = wait forever, the default for single-process services.
        [int]$TimeoutSeconds = 0
    )

    $elapsed = 0
    $lastDetail = ""
    $initialGracePeriod = 60  # 1 minute grace period before checking for crashes
    Write-Host "  Waiting for $ServiceName to be healthy..." -ForegroundColor Gray
    Write-Host "  Health check: $Url" -ForegroundColor DarkGray
    
    while ($true) {
        # If we know the launched process, detect an early exit
        # immediately (e.g. a config error) instead of waiting out the grace period.
        $launcherRunning = Test-ServiceLauncherRunning -Process $Process -LauncherProcessId $LauncherProcessId
        if (($Process -or $LauncherProcessId) -and -not $launcherRunning) {
            $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if (-not $listening) {
                Write-Host ""
                Write-Host "========================================" -ForegroundColor Red
                Write-Host "  ERROR: $ServiceName EXITED" -ForegroundColor Red
                Write-Host "========================================" -ForegroundColor Red
                Write-Host ""
                $codeText = if ($Process) {
                    if ($null -ne $Process.ExitCode) { $Process.ExitCode } else { "unknown" }
                } else { "unavailable for WMI launcher" }
                Write-Host "  The $ServiceName process exited (code $codeText) before becoming healthy." -ForegroundColor Red
                if ($LogPaths | Where-Object { $_ -and (Test-Path $_) }) {
                    Write-Host "  See the $ServiceName log below:" -ForegroundColor Yellow
                    Show-ServiceLogTail -LogPaths $LogPaths
                } else {
                    Write-Host "  Check the output above for error messages." -ForegroundColor Yellow
                }
                Write-Host ""
                return $false
            }
        }

        # After initial grace period, check if dependent services are still running
        if ($elapsed -ge $initialGracePeriod) {
            foreach ($depPort in $DependentPorts) {
                $depListening = Get-NetTCPConnection -LocalPort $depPort -State Listen -ErrorAction SilentlyContinue
                if (-not $depListening) {
                    Write-Host ""
                    Write-Host "========================================" -ForegroundColor Red
                    Write-Host "  ERROR: DEPENDENT SERVICE STOPPED" -ForegroundColor Red
                    Write-Host "========================================" -ForegroundColor Red
                    Write-Host ""
                    Write-Host "  Service on port $depPort is no longer running." -ForegroundColor Red
                    Write-Host "  Cannot continue starting $ServiceName." -ForegroundColor Yellow
                    Write-Host ""
                    Write-Host "  NOTE: $ServiceName might still be running in its terminal." -ForegroundColor DarkYellow
                    Write-Host "        Please check and close it manually if needed." -ForegroundColor DarkYellow
                    Write-Host ""
                    return $false
                }
            }
        }
        
        # After initial grace period, check if service crashed
        if ($elapsed -ge $initialGracePeriod -and $Port -gt 0) {
            $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if (-not $listening) {
                # Port not listening - check if the service process is still running
                $serviceRunning = $false
                
                # Get all python processes
                $pythonProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
                
                # Check for pip running (either as pip.exe or python -m pip)
                $pipProcs = Get-CimInstance Win32_Process -Filter "Name='pip.exe'" -ErrorAction SilentlyContinue
                if ($pipProcs) {
                    $serviceRunning = $true
                }
                
                # Also check for python running pip (python -m pip install ...)
                if (-not $serviceRunning) {
                    foreach ($proc in $pythonProcs) {
                        if ($proc.CommandLine -and ($proc.CommandLine -like "*pip*install*" -or $proc.CommandLine -like "*-m pip*")) {
                            $serviceRunning = $true
                            break
                        }
                    }
                }
                
                # Check for python with specific command line pattern (main.py, start_services.py)
                if (-not $serviceRunning -and $CommandLinePattern) {
                    foreach ($proc in $pythonProcs) {
                        if ($proc.CommandLine -and $proc.CommandLine -like "*$CommandLinePattern*") {
                            $serviceRunning = $true
                            break
                        }
                    }
                }
                
                # Check for npm/node processes (for Frontend)
                if (-not $serviceRunning) {
                    $npmProcs = Get-CimInstance Win32_Process -Filter "Name='npm.exe' OR Name='npm.cmd'" -ErrorAction SilentlyContinue
                    if ($npmProcs) {
                        $serviceRunning = $true
                    }
                }
                
                if (-not $serviceRunning) {
                    $nodeProcs = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue
                    foreach ($proc in $nodeProcs) {
                        # Check if node is running vite or npm
                        if ($proc.CommandLine -and ($proc.CommandLine -like "*vite*" -or $proc.CommandLine -like "*npm*" -or $proc.CommandLine -like "*5173*")) {
                            $serviceRunning = $true
                            break
                        }
                    }
                }

                # The launched wrapper stays alive through first-run model prep before main.py
                # opens the port.
                if (-not $serviceRunning -and $launcherRunning) {
                    $serviceRunning = $true
                }
                
                if (-not $serviceRunning) {
                    # No matching process running and port not listening = crashed or user closed terminal
                    Write-Host ""
                    Write-Host "========================================" -ForegroundColor Red
                    Write-Host "  ERROR: $ServiceName CRASHED" -ForegroundColor Red
                    Write-Host "========================================" -ForegroundColor Red
                    Write-Host ""
                    Write-Host "  No process is listening on port $Port." -ForegroundColor Red
                    if ($LogPaths | Where-Object { $_ -and (Test-Path $_) }) {
                        Write-Host "  See the $ServiceName log below:" -ForegroundColor Yellow
                        Show-ServiceLogTail -LogPaths $LogPaths
                    } else {
                        Write-Host "  Check the $ServiceName terminal for error messages." -ForegroundColor Yellow
                    }
                    Write-Host ""
                    return $false
                }
                # else: process still running (pip or python with matching command), keep waiting
            }
        }
        
        $detail = ""
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                Write-Host "  [$elapsed s] $ServiceName is healthy!" -ForegroundColor Green
                return $true
            }
        } catch {
            # Service not ready yet, continue waiting. A 503 from an aggregate
            # health endpoint tells us which sub-service is holding it up.
            $detail = Get-HealthDetail -ErrorRecord $_
        }
        $lastDetail = $detail

        # Newline-terminated, not an in-place `r overwrite: the Backend and
        # Content Search share this console and would append to an open line.
        Write-Host "  [$elapsed s] Waiting for $ServiceName...$detail" -ForegroundColor Gray
        Start-Sleep -Seconds $IntervalSeconds
        $elapsed += $IntervalSeconds

        if ($TimeoutSeconds -gt 0 -and $elapsed -ge $TimeoutSeconds) {
            Write-Host ""
            Write-Host "========================================" -ForegroundColor Red
            Write-Host "  ERROR: $ServiceName NOT READY" -ForegroundColor Red
            Write-Host "========================================" -ForegroundColor Red
            Write-Host ""
            Write-Host "  $ServiceName did not become healthy within ${TimeoutSeconds}s.$lastDetail" -ForegroundColor Red
            Write-Host "  Check the output above and content_search/logs/ for error messages." -ForegroundColor Yellow
            Show-ServiceLogTail -LogPaths $LogPaths
            Write-Host ""
            return $false
        }
    }
}

# Build proxy commands for child terminals
$proxyCommands = ""
if ($httpProxy) {
    $proxyCommands += "`$env:http_proxy='$httpProxy'; `$env:HTTP_PROXY='$httpProxy'; "
}
if ($httpsProxy) {
    $proxyCommands += "`$env:https_proxy='$httpsProxy'; `$env:HTTPS_PROXY='$httpsProxy'; "
}
if ($noProxy) {
    $proxyCommands += "`$env:no_proxy='$noProxy'; `$env:NO_PROXY='$noProxy'; "
}

# ============================================================================
# FRONTEND LAUNCH MODE
# ============================================================================
# Desktop-app mode exits long before this point, so the browser dev server is
# the only frontend this path starts.
# The tab does not inherit this script's environment (wt and WMI start it), so
# the proxy is passed in. Without it npm install goes direct, and behind a
# corporate proxy hangs on the registry for minutes before giving up.
$frontendProxyCommands = $proxyCommands
# Loopback only: the dev server proxies to the unauthenticated content-search and
# grading backends, so binding 0.0.0.0 would expose them to the whole network.
$frontendStartCommand = "npm run dev -- --host 127.0.0.1 --port 5173"
$frontendHeader = "FRONTEND UI"
$frontendStartMsg = "Starting Frontend (port 5173)..."
$frontendTitle = "Frontend"

if ($IsWindowsOS) {
    $wtExists = if ($NoWindowsTerminal) { $false } else { Get-Command wt -ErrorAction SilentlyContinue }

    # ========================================================================
    # LAYOUT MODEL PREPARATION (one-time; the layout & grading services
    # themselves are launched by the main app via GradingFeature.build())
    # ========================================================================
    if ($gradingEnabled) {
        $venvBackendPath = Join-Path (Split-Path $ScriptDir -Parent) "smartclassroom"
        $layoutDir = Join-Path $ScriptDir "components\grading\providers\layout_detection_service"
        $layoutIrModel = Join-Path $ScriptDir "models\detection_model\PP-DocLayoutV2-ov\fp16\model.xml"

        # The layout IR model needs a separate conversion venv (paddle2onnx
        # conflicts with the main venv). Prepared once when the IR is absent;
        # subsequent starts skip this and the main app launches the service.
        if (-not (Test-Path $layoutIrModel)) {
            Write-Host ""
            Write-Host "Preparing layout model (one-time, may take several minutes)..." -ForegroundColor Yellow

            $convertVenv = Join-Path $layoutDir "venv_convert"
            if (-not (Test-Path (Join-Path $convertVenv "Scripts\paddle2onnx.exe"))) {
                Write-Host "  Creating conversion venv..." -ForegroundColor Gray
                & "$venvBackendPath\Scripts\python.exe" -m venv $convertVenv
                & "$convertVenv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
                & "$convertVenv\Scripts\pip.exe" install -r (Join-Path $layoutDir "requirements_convert.txt")
            }

            Write-Host "  Downloading and converting layout model..." -ForegroundColor Gray
            Push-Location $layoutDir
            & "$venvBackendPath\Scripts\python.exe" ensure_layout_model.py
            $ensureExit = $LASTEXITCODE
            Pop-Location
            if ($ensureExit -ne 0 -or -not (Test-Path $layoutIrModel)) {
                Write-Host "Exiting script: layout model preparation failed." -ForegroundColor Red
                exit 1
            }
            Write-Host "  Layout model ready." -ForegroundColor Green
        }
    }

    # ========================================================================
    # BACKEND (runs in THIS terminal, with paddleocr check)
    # ========================================================================
    if ($script:skipBackend) {
        Write-Host "Skipping Backend (already running on port 8000)" -ForegroundColor Yellow
    } else {
        Write-Host "Starting Backend in this terminal..." -ForegroundColor Yellow
        
        
        $backendScript = @"
`$ErrorActionPreference = 'Continue'
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# Set proxy
$proxyCommands

Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  BACKEND SERVICE' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan
Write-Host ''

`$parentDir = Split-Path '$ScriptDir' -Parent
Set-Location `$parentDir
Write-Host "Working directory: `$PWD" -ForegroundColor Gray
Write-Host ''

# Check if venv exists and is valid
`$venvPath = '.\smartclassroom'
`$venvValid = (Test-Path "`$venvPath\Scripts\Activate.ps1") -and (Test-Path "`$venvPath\Scripts\python.exe")

if (-not `$venvValid) {
    # Remove broken/partial venv if exists
    if (Test-Path `$venvPath) {
        Write-Host 'Removing incomplete smartclassroom venv...' -ForegroundColor Yellow
        Remove-Item -Path `$venvPath -Recurse -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    
    Write-Host 'Creating smartclassroom virtual environment...' -ForegroundColor Yellow
    python -m venv `$venvPath
    if (`$LASTEXITCODE -ne 0) {
        Write-Host 'Failed to create virtual environment!' -ForegroundColor Red
        Write-Host 'Try running: Remove-Item -Path smartclassroom -Recurse -Force' -ForegroundColor Yellow
        Read-Host 'Press Enter to close'
        exit 1
    }
}

Write-Host 'Activating virtual environment...' -ForegroundColor Gray
& "`$venvPath\Scripts\Activate.ps1"

Set-Location '$ScriptDir'
Write-Host "Changed to: `$PWD" -ForegroundColor Gray

`$prepareContentSearchModels = [System.Convert]::ToBoolean('$contentSearchEnabled')
if (`$prepareContentSearchModels) {
    Write-Host ''
    Write-Host 'Preparing Content Search embedding models...' -ForegroundColor Yellow
    `$previousPythonPath = `$env:PYTHONPATH
    `$env:PYTHONUNBUFFERED = '1'
    `$env:PYTHONIOENCODING = 'utf-8'
    `$env:PYTHONUTF8 = '1'
    `$env:PYTHONPATH = '$ScriptDir\content_search;$ScriptDir'
    if (`$previousPythonPath) {
        `$env:PYTHONPATH += ";`$previousPythonPath"
    }

    Push-Location '$ScriptDir\content_search'
    try {
        Write-Host '  Preparing visual embedding model...' -ForegroundColor Gray
        python -c "from start_services import _load_config_to_env; _load_config_to_env(); from providers.file_ingest_and_retrieve.models import get_visual_embedding_model; get_visual_embedding_model()"
        if (`$LASTEXITCODE -ne 0) {
            Write-Host 'Visual embedding model preparation failed.' -ForegroundColor Red
            exit 1
        }

        Write-Host '  Preparing document embedding model...' -ForegroundColor Gray
        python -c "from start_services import _load_config_to_env; _load_config_to_env(); from providers.file_ingest_and_retrieve.models import get_document_embedding_model; get_document_embedding_model()"
        if (`$LASTEXITCODE -ne 0) {
            Write-Host 'Document embedding model preparation failed.' -ForegroundColor Red
            exit 1
        }
    } finally {
        Pop-Location
        `$env:PYTHONPATH = `$previousPythonPath
    }

    Write-Host 'Content Search embedding models are ready.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Starting Backend Service (port 8000)...' -ForegroundColor Green
Write-Host ''
python main.py
"@
    $backendEncoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($backendScript))

    # Detached mode has no console, so capture the backend's output to files we
    # can tail on failure; windowed mode shares this console instead.
    $script:backendLogPath = $null
    $script:backendErrPath = $null
    $script:backendProcess = $null
    $script:backendProcessId = 0

    if ($NoWindowsTerminal) {
        $backendLogDir = Join-Path $ScriptDir "logs"
        if (-not (Test-Path $backendLogDir)) { New-Item -ItemType Directory -Path $backendLogDir -Force | Out-Null }
        $backendTs = Get-Date -Format "yyyyMMdd_HHmmss"
        $script:backendLogPath = Join-Path $backendLogDir "backend_$backendTs.log"
        $script:backendErrPath = Join-Path $backendLogDir "backend_$backendTs.err"

        $script:backendProcessId = Start-DetachedBackend -BackendScript $backendScript `
            -LogPath $script:backendLogPath -ErrorPath $script:backendErrPath
    } else {
        $script:backendProcess = Start-Process powershell -NoNewWindow -PassThru -ArgumentList "-ExecutionPolicy Bypass -EncodedCommand $backendEncoded"
    }

    if ($NoWindowsTerminal) {
        Write-Host "  Backend started through WMI (PID $script:backendProcessId), independent of this session" -ForegroundColor Green
        Write-Host "  Backend output -> $script:backendLogPath" -ForegroundColor DarkGray
    } else {
        Write-Host "  Backend started in this terminal" -ForegroundColor Green
    }
    Write-Host ""
    } 
    
    $backendHealthy = Wait-ForService -ServiceName "Backend" -Url "http://localhost:8000/health" -Port 8000 -CommandLinePattern "main.py" -Process $script:backendProcess -LauncherProcessId $script:backendProcessId -LogPaths @($script:backendLogPath, $script:backendErrPath)
    if (-not $backendHealthy) {
        Write-Host "Exiting script due to Backend startup failure." -ForegroundColor Red
        exit 1
    }
    
  
    if ($contentSearchEnabled) {
        Write-Host ""
        Write-Host "Content Search is started by the backend (main.py); waiting for it to become healthy..." -ForegroundColor Yellow
        Write-Host "File Ingest loads the embedding/reranker models - this can take a few minutes on first start." -ForegroundColor Yellow

        # One gate for the whole content-search stack: /api/v1/system/health on
        # :9011 answers 200 only once every service start_services.py launches
        # (chromadb 9090, video preprocess 8001, file ingest 9990, main_app
        # itself) is ready, and 503 with the per-service detail until then. That
        # matches "[launcher] All N services are ready" in the backend output.
        $csHealthy = Wait-ForService -ServiceName "Content Search" -Url "http://localhost:9011/api/v1/system/health" -Port 9011 -DependentPorts @(8000) -CommandLinePattern "start_services.py" -TimeoutSeconds 1800
        if (-not $csHealthy) {
            Write-Host "Exiting script due to Content Search startup failure." -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host ""
        Write-Host "Content Search is disabled in config (content_search/topic_segmentation/qa all off); skipping." -ForegroundColor Gray
    }

    # Grading service (port 9012) is launched by the main app via
    # GradingFeature.build(); no separate terminal needed here.

    # ========================================================================
    # TERMINAL 4: FRONTEND
    # ========================================================================
    if ($script:skipFrontend) {
        Write-Host ""
        Write-Host "Skipping Frontend (already running on port 5173)" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "Launching Terminal: $frontendTitle..." -ForegroundColor Yellow

        $frontendScript = @"
`$ErrorActionPreference = 'Continue'

# Proxy for npm's registry access
$frontendProxyCommands

Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  $frontendHeader' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan
Write-Host ''

Set-Location '$ScriptDir\ui'
Write-Host "Working directory: `$PWD" -ForegroundColor Gray
Write-Host ''

# npm writes node_modules\.package-lock.json at the end of every install; newer
# than package.json and package-lock.json means nothing changed since. A no-op
# install still takes 10+ s of registry round trips (audit included).
`$installed = Get-Item 'node_modules\.package-lock.json' -ErrorAction SilentlyContinue
`$manifests = Get-Item 'package.json', 'package-lock.json' -ErrorAction SilentlyContinue
if (`$installed -and -not (`$manifests | Where-Object { `$_.LastWriteTime -gt `$installed.LastWriteTime })) {
    Write-Host 'npm dependencies up to date, skipping npm install.' -ForegroundColor Gray
} else {
    Write-Host 'Installing npm dependencies...' -ForegroundColor Yellow
    npm install --no-audit --no-fund
}

Write-Host ''
Write-Host '$frontendStartMsg' -ForegroundColor Green
Write-Host ''
$frontendStartCommand
"@
    $frontendEncoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($frontendScript))

    if ($wtExists) {
        # A tab beside the backend when this script runs in Windows Terminal
        # (-w 0: the current window), otherwise a window of its own.
        $wtWindow = if ($env:WT_SESSION) { "0" } else { "SmartClassroom" }
        Start-Process wt -ArgumentList "-w $wtWindow new-tab --title $frontendTitle powershell -NoExit -EncodedCommand $frontendEncoded"
    } else {
        Invoke-WmiMethod -Path win32_process -Name create -ArgumentList "powershell.exe -ExecutionPolicy Bypass -EncodedCommand $frontendEncoded" | Out-Null
    }

    Write-Host "  $frontendTitle terminal launched" -ForegroundColor Green
    Write-Host ""
    }  # End of skipFrontend check
    
    # Wait for Frontend to be healthy
    $frontendDeps = if ($gradingEnabled) { @(8000, 9011, 9012) } else { @(8000, 9011) }
    # 127.0.0.1, the address Vite binds: "localhost" tries ::1 first, and the
    # refused IPv6 connect costs about 2 s on every probe.
    $frontendHealthy = Wait-ForService -ServiceName "Frontend" -Url "http://127.0.0.1:5173" -Port 5173 -DependentPorts $frontendDeps -CommandLinePattern "npm"
    if (-not $frontendHealthy) {
        Write-Host "Exiting script due to Frontend startup failure." -ForegroundColor Red
        exit 1
    }
}

# ============================================================================
# COMPLETION MESSAGE
# ============================================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "   ALL SERVICES ARE HEALTHY!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Services:" -ForegroundColor Yellow
Write-Host "  1. Backend        -> http://localhost:8000  [HEALTHY]" -ForegroundColor White
if ($contentSearchEnabled) {
    Write-Host "  2. Content Search -> http://localhost:9011  [HEALTHY]" -ForegroundColor White
    Write-Host "       File Ingest  -> http://localhost:9990  [HEALTHY]" -ForegroundColor White
    if ($videoSummarizationEnabled) {
        Write-Host "       Preprocess   -> http://localhost:8001  [HEALTHY]" -ForegroundColor White
    }
} else {
    Write-Host "  2. Content Search -> disabled in config" -ForegroundColor Gray
}
Write-Host "  3. Frontend       -> http://localhost:5173  [HEALTHY]" -ForegroundColor White
Write-Host ""
Write-Host "Open in browser: http://localhost:5173" -ForegroundColor Cyan
Write-Host ""
Write-Host "DEPRECATED: the browser UI will be removed after 2027.0.0." -ForegroundColor Yellow
Write-Host "Use the desktop app instead: ./start-desktop-app.ps1" -ForegroundColor Yellow
Write-Host ""

if ($Silent) {
    Write-Host "Silent mode: services started successfully. Exiting..." -ForegroundColor Green
    Write-Host ""
    $script:servicesStarted = $false  
    exit 0
} else {
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "  Press Ctrl+C to stop all services and exit." -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ""

    try {
        while ($true) {
            if (($script:backendProcess -or $script:backendProcessId) -and
                -not (Test-ServiceLauncherRunning -Process $script:backendProcess -LauncherProcessId $script:backendProcessId)) {
                Write-Host ""
                Write-Host "Backend launcher exited. Stopping remaining services..." -ForegroundColor Yellow
                break
            }
            Start-Sleep -Seconds 1
        }
    }
    finally {
        if ($script:servicesStarted) {
            Stop-AllServices
            $script:servicesStarted = $false
        }
    }
}