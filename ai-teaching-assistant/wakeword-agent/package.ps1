# =============================================================================
# package.ps1  -  Build + sign an MSIX for the ATA Wake Agent.
#
# Produces a signed, sideloadable MSIX using MakeAppx + SignTool from the
# Microsoft.Windows.SDK.BuildTools NuGet package (no full Windows SDK needed).
#
#   .\package.ps1            # publish + pack + sign  (no admin needed)
#   .\package.ps1 -Install   # also trust the cert + install (needs admin)
#
# The signed .msix lands in packaging\out\. On the hardware wake-enabled device, run with
# -Install (elevated), then set ATA_WAKEWORD_AGENT_AUMID in .env.
# =============================================================================
param(
    [switch]$Install = $false,
    # Keep the existing aarconfig binding; rebinding restarts AarSvc, which then needs a reboot.
    [switch]$SkipKeywordBinding = $false
)

$ErrorActionPreference = "Stop"
$AgentDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PkgDir     = Join-Path $AgentDir "packaging"
$Stage      = Join-Path $PkgDir "stage"
$OutDir     = Join-Path $PkgDir "out"
$Msix       = Join-Path $OutDir "AtaWakeAgent.msix"
$PfxPath    = Join-Path $PkgDir "ata-wake-agent-dev.pfx"
$HostProject = Join-Path $AgentDir "activation-host\AtaWakeActivationHost.csproj"
# Only the Debug CoreCLR framework (Microsoft.NET.CoreFramework.Debug.2.2) is present on
# target machines, so the managed UWP host is built Debug to match the installed runtime.
$HostConfig = "Debug"
$HostOutput = Join-Path $AgentDir "activation-host\bin\x64\$HostConfig"
$HostAssets = Join-Path $AgentDir "activation-host\obj\PackageAssets"
$PublisherCN = "CN=ATAWakeAgentDev"   # must match Package.appxmanifest Identity Publisher
$elevated = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Install -and -not $elevated) {
    throw "-Install requires an elevated PowerShell. Reopen PowerShell as Administrator and rerun package.ps1 -Install."
}

function Find-Tool($name) {
    # Prefer the installed Windows SDK (reliable); fall back to the NuGet buildtools.
    $kits = "C:\Program Files (x86)\Windows Kits\10\bin"
    $t = Get-ChildItem $kits -Recurse -Filter $name -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $t) {
        $t = Get-ChildItem (Join-Path $PkgDir "packages\microsoft.windows.sdk.buildtools") -Recurse -Filter $name -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $t) { throw "$name not found. Install the Windows SDK, or run: dotnet restore packaging\tools.csproj" }
    return $t
}

$MakeAppx = Find-Tool "makeappx.exe"
$SignTool = Find-Tool "signtool.exe"
$MsBuild = Get-Command "msbuild.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
if (-not $MsBuild) {
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $VsWhere) {
        $MsBuild = & $VsWhere -latest -products '*' -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
    }
}
if (-not $MsBuild) {
    throw "MSBuild not found. Install Visual Studio 2022 with the Universal Windows Platform development workload and Windows 10 SDK."
}

# The UWP manifest validates logo payload files during its build.
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force $HostAssets | Out-Null
function New-Logo($path, $size) {
    $bitmap = New-Object System.Drawing.Bitmap $size, $size
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear([System.Drawing.Color]::FromArgb(0, 120, 212))
    $graphics.Dispose()
    $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
}
New-Logo (Join-Path $HostAssets "Square44x44Logo.png") 44
New-Logo (Join-Path $HostAssets "Square150x150Logo.png") 150
New-Logo (Join-Path $HostAssets "StoreLogo.png") 50

# --- 1. Publish the full-trust worker (must precede the host build so it is packaged) ---
Write-Host "[*] Publishing agent..." -ForegroundColor Cyan
$Publish = Join-Path $AgentDir "bin\Release\net8.0-windows10.0.26100.0\win-x64\publish"
dotnet publish (Join-Path $AgentDir "AtaWakeAgent.csproj") -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=false | Out-Null
if (-not (Test-Path (Join-Path $Publish "AtaWakeAgent.exe"))) { throw "publish failed: AtaWakeAgent.exe missing" }
if (Get-ChildItem -LiteralPath $Publish -Recurse -Filter 'wake-access.local.json' -File) {
    throw 'Private access configuration found in publish output. Remove the stale build output before packaging; credentials must not be included in the MSIX.'
}

# --- 2. Resolve the package version (bump above any installed build on -Install) ---
$SourceManifest = Join-Path $AgentDir "Package.appxmanifest"
$manifestXml = [xml](Get-Content -Raw $SourceManifest)
$originalVersion = $manifestXml.Package.Identity.Version
$packageVersion = [version]$originalVersion
if ($Install) {
    $installedVersions = @(Get-AppxPackage -AllUsers -Name "Intel.ATA.WakeAgent" -ErrorAction SilentlyContinue |
        ForEach-Object { [version]$_.Version } | Sort-Object -Descending)
    if ($installedVersions.Count -gt 0 -and $installedVersions[0] -ge $packageVersion) {
        $cur = $installedVersions[0]
        if ($cur.Revision -lt 65535) { $packageVersion = [version]::new($cur.Major, $cur.Minor, $cur.Build, $cur.Revision + 1) }
        elseif ($cur.Build -lt 65535) { $packageVersion = [version]::new($cur.Major, $cur.Minor, $cur.Build + 1, 0) }
        elseif ($cur.Minor -lt 65535) { $packageVersion = [version]::new($cur.Major, $cur.Minor + 1, 0, 0) }
        else { throw "Cannot increment installed package version $cur." }
    }
}

# --- 3. Build the UWP package: the toolchain emits the native apphost, resources.pri,
#        framework references and the worker as one correct MSIX. Hand-assembly fails
#        because a bare managed .NET Core UWP exe has no apphost and cannot launch. ---
Write-Host "[*] Building UWP package ($HostConfig) v$packageVersion..." -ForegroundColor Cyan
$AppPackages = Join-Path $AgentDir "activation-host\AppPackages"
if (Test-Path $AppPackages) { Remove-Item $AppPackages -Recurse -Force }
$manifestXml.Package.Identity.SetAttribute("Version", $packageVersion.ToString())
$manifestXml.Save($SourceManifest)
try {
    & $MsBuild $HostProject /restore /t:Build `
        /p:Configuration=$HostConfig /p:Platform=x64 `
        /p:AppxPackage=true /p:AppxBundle=Never `
        /p:UapAppxPackageBuildMode=SideloadOnly /p:AppxPackageSigningEnabled=false /v:minimal /nologo
    if ($LASTEXITCODE -ne 0) { throw "UWP package build failed" }
}
finally {
    # Keep the checked-in manifest version out of the build bump.
    $manifestXml.Package.Identity.SetAttribute("Version", $originalVersion)
    $manifestXml.Save($SourceManifest)
}
Write-Host "[*] MSIX package version: $packageVersion" -ForegroundColor Cyan

$built = Get-ChildItem $AppPackages -Recurse -Filter "*.msix" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $built) { throw "build did not produce an .msix under $AppPackages" }
New-Item -ItemType Directory -Force $OutDir | Out-Null
Copy-Item $built.FullName $Msix -Force

# --- 5. Dev signing certificate (create once, reuse) ---
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $PublisherCN } | Select-Object -First 1
if (-not $cert) {
    Write-Host "[*] Creating self-signed dev cert $PublisherCN..." -ForegroundColor Cyan
    $cert = New-SelfSignedCertificate -Type Custom -Subject $PublisherCN -KeyUsage DigitalSignature `
        -FriendlyName "ATA Wake Agent Dev" -CertStoreLocation "Cert:\CurrentUser\My" `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
}
$pfxPw = ConvertTo-SecureString -String "ata-dev" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $pfxPw | Out-Null

# --- 6. Sign ---
Write-Host "[*] Signing MSIX..." -ForegroundColor Cyan
& $SignTool sign /fd SHA256 /a /f $PfxPath /p "ata-dev" $Msix
if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
Write-Host "[OK] Signed package: $Msix" -ForegroundColor Green

# --- 7. Optional install (needs admin to trust the cert machine-wide) ---
if ($Install) {
    Write-Host "[*] Trusting cert + installing package..." -ForegroundColor Cyan
    Import-PfxCertificate -FilePath $PfxPath -Password $pfxPw -CertStoreLocation "Cert:\LocalMachine\TrustedPeople" | Out-Null
    # Debug builds depend on the Debug VCLibs framework; supply it from the SDK if absent.
    $deps = @()
    if (-not (Get-AppxPackage -Name "Microsoft.VCLibs.140.00.Debug" -ErrorAction SilentlyContinue)) {
        $vclibsDebug = Get-ChildItem "C:\Program Files (x86)\Microsoft SDKs\Windows Kits\10\ExtensionSDKs\Microsoft.VCLibs\14.0\Appx\Debug\x64" -Filter "*.appx" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
        if ($vclibsDebug) { $deps += $vclibsDebug } else { throw "Microsoft.VCLibs.140.00.Debug framework not found; install the Windows SDK or VCLibs." }
    }
    if ($deps.Count -gt 0) {
        Add-AppxPackage -Path $Msix -DependencyPath $deps -ForceApplicationShutdown
    }
    else {
        Add-AppxPackage -Path $Msix -ForceApplicationShutdown
    }
    $pkg = Get-AppxPackage -Name "Intel.ATA.WakeAgent"
    $aumid = "$($pkg.PackageFamilyName)!AtaWakeAgent"
    $EnvFile = Join-Path (Split-Path $AgentDir -Parent) ".env"
    if (Test-Path $EnvFile) {
        $envLines = @(Get-Content $EnvFile)
        $values = @{
            "ATA_WAKEWORD_BACKEND" = "hardware"
            "ATA_WAKEWORD_AGENT_AUMID" = $aumid
        }
        foreach ($key in $values.Keys) {
            $pattern = "^\s*$([regex]::Escape($key))="
            if ($envLines -match $pattern) {
                $envLines = $envLines | ForEach-Object { if ($_ -match $pattern) { "$key=$($values[$key])" } else { $_ } }
            }
            else {
                $envLines += "$key=$($values[$key])"
            }
        }
        Set-Content -Path $EnvFile -Value $envLines
    }
    Write-Host "[OK] Installed. AUMID: $aumid" -ForegroundColor Green
    Write-Host "[*] Configure private access for this user with configure-access.ps1 before arming the host. See README.md." -ForegroundColor Cyan
    if ($SkipKeywordBinding) {
        Write-Host "[*] Keeping the existing keyword binding; starting the host once..." -ForegroundColor Cyan
        Start-Process "shell:AppsFolder\$aumid"
    }
    else {
        Write-Host "[*] Binding the DSP keyword to ATA (aarconfig + AarSvc restart)..." -ForegroundColor Cyan
        & (Join-Path $AgentDir "register-keyword.ps1")
        Write-Host "[!] Reboot, then open the host once, before saying the keyword." -ForegroundColor Yellow
    }
}
