# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

<#
.SYNOPSIS
    Runs the Smart Classroom setup tasks that need administrator rights, all in
    one elevated process, so Windows asks for approval once.

.DESCRIPTION
    The app starts this script with -Launch -RunAs and a plan file it wrote
    (electron/services/elevation.cjs). The script starts itself again,
    elevated, which is the one prompt, and the app follows the log the
    elevated run writes next to the plan. The prompt names Windows PowerShell.

    Tasks, in the plan's order:
      longPaths   Enables Windows long paths (HKLM LongPathsEnabled = 1).
      dlStreamer  Runs check_dlstreamer.ps1 -Install, which downloads and runs
                  the DL Streamer installer. Started from this elevated process,
                  the installer does not prompt again. It downloads into a new
                  folder only administrators can write, so nothing can swap the
                  installer before it runs.

    The plan carries data only. The script it runs is the one next to this
    one, and the log, result and stop files are in the plan's folder.

    Plan (JSON):
      { "tasks": [ { "id": "longPaths" },
                   { "id": "dlStreamer", "requiredVersion": "2026.1.0",
                     "httpProxy": "", "httpsProxy": "" } ] }

    Written to the plan's folder:
      admin-tasks.log  Output, one line each. Lines starting "@@admin " are
                       events for the app, as JSON: {"event":"started"},
                       {"event":"task","task":<id>,"state":"running"|"ok"|
                       "failed"|"skipped","message":<text>}, {"event":"done"}.
      result.json      {"dryRun":<bool>,"tasks":{<id>:{"ok":<bool>,
                       "error":<text>,"exitCode":<int>}}}

    A file named "stop" in the plan's folder skips the tasks not yet started:
    the app's Cancel. A running task is not interrupted.

.PARAMETER Plan
    The plan file.

.PARAMETER Launch
    What the app runs, unelevated: start this script again in a new hidden
    process, with the same plan, and wait for it. The exit code is that run's,
    1223 when the administrator prompt was declined, or 2 when it could not
    start. An elevated process cannot pipe its output back, hence the log.

.PARAMETER RunAs
    With -Launch, ask Windows to start that process elevated: the one prompt.
    Without it, the process runs unelevated; for tests.

.PARAMETER DryRun
    Reads the plan and reports each task as done without changing anything.
    Needs no administrator rights; for tests.

.OUTPUTS
    Exit code 0 when every task succeeded, 1 otherwise.

.EXAMPLE
    # What the app runs:
    & .\Scripts\run_admin_tasks.ps1 -Plan <folder>\plan.json -Launch -RunAs
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,
    [switch]$Launch,
    [switch]$RunAs,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ($Launch) {
    # Quoted for the command line; a Windows path cannot hold a double quote.
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"", '-Plan', "`"$([System.IO.Path]::GetFullPath($Plan))`"")
    if ($DryRun) { $arguments += '-DryRun' }

    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $info.Arguments = $arguments -join ' '
    $info.UseShellExecute = $true
    $info.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    if ($RunAs) { $info.Verb = 'runas' }
    # Process.Start rather than Start-Process: the Win32Exception that says the
    # prompt was declined reaches the catch, where Start-Process keeps only
    # its message.
    try {
        $process = [System.Diagnostics.Process]::Start($info)
    } catch {
        $e = $_.Exception
        $message = $e.Message
        while ($e) {
            # ERROR_CANCELLED: the prompt was declined.
            if ($e -is [System.ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 1223) { exit 1223 }
            $message = $e.Message
            $e = $e.InnerException
        }
        [Console]::Error.WriteLine($message)
        exit 2
    }
    if (-not $process) { exit 2 }
    # Holds the handle, so the exit code is still there once it exits.
    $null = $process.Handle
    $process.WaitForExit()
    exit $process.ExitCode
}

$planDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($Plan))
$logPath = Join-Path $planDir 'admin-tasks.log'
$resultPath = Join-Path $planDir 'result.json'
$stopPath = Join-Path $planDir 'stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$Line) {
    [System.IO.File]::AppendAllText($logPath, $Line + "`n", $utf8)
}

function Write-Event([System.Collections.IDictionary]$Fields) {
    Write-Log ('@@admin ' + ($Fields | ConvertTo-Json -Compress))
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# A folder only administrators and SYSTEM can write. Under the Windows temp
# folder, where users cannot rename or replace what an administrator created.
function New-AdminFolder {
    $dir = Join-Path $env:windir ('Temp\SmartClassroom-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null
    # By SID, so the names do not depend on the Windows language.
    & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to $dir." }
    return $dir
}

function Invoke-LongPaths {
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    if ($DryRun) {
        Write-Log 'Dry run: would set LongPathsEnabled to 1.'
        return @{ ok = $true }
    }
    Set-ItemProperty -Path $key -Name LongPathsEnabled -Value 1 -Type DWord -ErrorAction Stop
    if ((Get-ItemProperty -Path $key -Name LongPathsEnabled).LongPathsEnabled -ne 1) {
        throw 'Long paths are still disabled after setting them.'
    }
    Write-Log 'Windows long paths are enabled.'
    return @{ ok = $true }
}

function Invoke-DLStreamer($Task) {
    $script = Join-Path $PSScriptRoot 'check_dlstreamer.ps1'
    if (-not (Test-Path $script)) { throw "$script is missing." }

    $version = [string]$Task.requiredVersion
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Not a DL Streamer version: '$version'." }
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $script,
        '-Install', '-Yes', '-RequiredVersion', $version)
    foreach ($name in @('HttpProxy', 'HttpsProxy')) {
        $value = [string]$Task.($name.Substring(0, 1).ToLower() + $name.Substring(1))
        if (-not $value) { continue }
        # Passed on as an argument, so nothing that could end it early. The
        # rule the app's Configuration screen applies (config-schema.cjs).
        if ($value -notmatch '^https?://[^\s"'']+$') { throw "The $name setting is not a proxy URL." }
        $arguments += @("-$name", $value)
    }

    if ($DryRun) {
        Write-Log "Dry run: would install DL Streamer $version."
        return @{ ok = $true }
    }

    $downloadDir = New-AdminFolder
    try {
        $arguments += @('-DownloadDir', $downloadDir)
        $powershell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
        # Native output merged with 2>&1 turns into errors under 'Stop'.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $powershell @arguments 2>&1 | ForEach-Object { Write-Log ([string]$_) }
        } finally {
            $ErrorActionPreference = $previous
        }
        $code = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $downloadDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) {
        return @{ ok = $false; exitCode = $code; error = "check_dlstreamer.ps1 exited with code $code." }
    }
    return @{ ok = $true }
}

$results = [ordered]@{}
$allOk = $true
try {
    # Whether Windows did elevate it, for the log and the elevated dry run.
    Write-Event ([ordered]@{ event = 'started'; dryRun = [bool]$DryRun; admin = [bool](Test-Administrator) })

    $planData = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
    $tasks = @($planData.tasks | Where-Object { $_ })
    $elevated = $DryRun -or (Test-Administrator)

    foreach ($task in $tasks) {
        $id = [string]$task.id
        if (Test-Path -LiteralPath $stopPath) {
            $results[$id] = @{ ok = $false; error = 'Cancelled before it started.'; skipped = $true }
            Write-Event ([ordered]@{ event = 'task'; task = $id; state = 'skipped'; message = 'Cancelled before it started.' })
            continue
        }

        Write-Event ([ordered]@{ event = 'task'; task = $id; state = 'running' })
        try {
            if (-not $elevated) { throw 'This needs administrator rights, and the helper is not running with them.' }
            $outcome = switch ($id) {
                'longPaths' { Invoke-LongPaths }
                'dlStreamer' { Invoke-DLStreamer $task }
                default { throw "Unknown task '$id'." }
            }
        } catch {
            $outcome = @{ ok = $false; error = $_.Exception.Message }
        }

        $results[$id] = $outcome
        if ($outcome.ok) {
            Write-Event ([ordered]@{ event = 'task'; task = $id; state = 'ok' })
        } else {
            Write-Log "$id failed: $($outcome.error)"
            Write-Event ([ordered]@{ event = 'task'; task = $id; state = 'failed'; message = $outcome.error })
        }
    }
} catch {
    Write-Log "The administrator tasks stopped: $($_.Exception.Message)"
    $results['_helper'] = @{ ok = $false; error = $_.Exception.Message }
} finally {
    foreach ($entry in $results.Values) { if (-not $entry.ok) { $allOk = $false } }
    $result = [ordered]@{ dryRun = [bool]$DryRun; tasks = $results }
    [System.IO.File]::WriteAllText($resultPath, ($result | ConvertTo-Json -Depth 4 -Compress), $utf8)
    Write-Event ([ordered]@{ event = 'done'; ok = $allOk })
}

if ($allOk) { exit 0 }
exit 1
