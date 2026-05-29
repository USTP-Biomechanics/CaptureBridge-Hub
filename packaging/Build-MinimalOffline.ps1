param(
    [string]$OutputDir = "",
    [string]$PythonVersion = "3.12.10",
    [string]$PythonInstallerPath = "",
    [string]$SourcePythonDir = "",
    [switch]$UseInstaller,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not $OutputDir) {
    $OutputDir = Join-Path $repoRoot "dist"
} elseif ($OutputDir -notmatch "^[A-Za-z]:\\|^\\\\") {
    $OutputDir = Join-Path $repoRoot $OutputDir
}

$packageName = "CaptureBridge_Hub_Minimal_Offline"
$stageParent = Join-Path $repoRoot "build\minimal-offline"
$stageRoot = Join-Path $stageParent $packageName
$appDir = Join-Path $stageRoot "app"
$pythonDir = Join-Path $stageRoot "python"
$downloadsDir = Join-Path $repoRoot "build\downloads"
$zipPath = Join-Path $OutputDir "$packageName.zip"

function Remove-DirectoryIfExists {
    param(
        [string]$Path,
        [string]$AllowedRoot
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $allowed = (Resolve-Path -LiteralPath $AllowedRoot).Path
    if ($resolved -notlike "$allowed\*") {
        throw "Refusing to remove path outside allowed root: $resolved"
    }

    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Copy-RequiredFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Missing required file: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Download-RequiredFile {
    param(
        [string]$Uri,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }

    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe -L --fail --retry 3 --output $Destination $Uri
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $Destination)) {
            return
        }
        if (Test-Path -LiteralPath $Destination) {
            Remove-Item -LiteralPath $Destination -Force
        }
    }

    Invoke-WebRequest -Uri $Uri -OutFile $Destination -UseBasicParsing
}

function Get-AutoSourcePythonDir {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        return ""
    }

    $basePrefix = & $venvPython -c "import sys; print(sys.base_prefix)"
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    if (-not $basePrefix) {
        return ""
    }

    $candidate = Join-Path $basePrefix "python.exe"
    if (Test-Path -LiteralPath $candidate) {
        return $basePrefix
    }

    return ""
}

function Copy-PythonPackageFromSitePackages {
    param(
        [string]$SourceSitePackages,
        [string]$DestinationSitePackages,
        [string]$PackageDirectory,
        [string]$DistInfoPattern
    )

    $packageSource = Join-Path $SourceSitePackages $PackageDirectory
    if (-not (Test-Path -LiteralPath $packageSource)) {
        return $false
    }

    New-Item -ItemType Directory -Force -Path $DestinationSitePackages | Out-Null
    Copy-Item -LiteralPath $packageSource -Destination (Join-Path $DestinationSitePackages $PackageDirectory) -Recurse -Force

    Get-ChildItem -LiteralPath $SourceSitePackages -Directory -Filter $DistInfoPattern -ErrorAction SilentlyContinue |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $DestinationSitePackages $_.Name) -Recurse -Force
        }

    if ($PackageDirectory -eq "PIL") {
        $pillowLibs = Join-Path $SourceSitePackages "pillow.libs"
        if (Test-Path -LiteralPath $pillowLibs) {
            Copy-Item -LiteralPath $pillowLibs -Destination (Join-Path $DestinationSitePackages "pillow.libs") -Recurse -Force
        }
    }

    return $true
}

Write-Host "Building $packageName"
Write-Host "Repo root: $repoRoot"

New-Item -ItemType Directory -Force -Path $stageParent | Out-Null
Remove-DirectoryIfExists -Path $stageRoot -AllowedRoot $stageParent
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $downloadsDir | Out-Null

Write-Host ""
Write-Host "Copying minimal app files..."
Copy-RequiredFile -Source (Join-Path $repoRoot "tcp_arduino_sync.py") -Destination $appDir
Copy-RequiredFile -Source (Join-Path $repoRoot "phone_stream.py") -Destination $appDir
Copy-RequiredFile -Source (Join-Path $repoRoot "requirements.txt") -Destination $appDir
Copy-RequiredFile -Source (Join-Path $scriptDir "minimal_app_config.json") -Destination (Join-Path $appDir "app_config.json")
Copy-RequiredFile -Source (Join-Path $scriptDir "Run_MinimalOffline.bat") -Destination (Join-Path $stageRoot "Run_CaptureBridge_Hub.bat")
Copy-RequiredFile -Source (Join-Path $scriptDir "README_Minimal_Offline.txt") -Destination (Join-Path $stageRoot "README_Minimal_Offline.txt")
Copy-RequiredFile -Source (Join-Path $scriptDir "app-release.apk") -Destination (Join-Path $stageRoot "app-release.apk")
Copy-RequiredFile -Source (Join-Path $repoRoot "README.md") -Destination (Join-Path $stageRoot "README.md")

$docsSource = Join-Path $repoRoot "docs"
if (Test-Path -LiteralPath $docsSource) {
    Copy-Item -LiteralPath $docsSource -Destination (Join-Path $stageRoot "docs") -Recurse -Force
}

$arduinoSource = Join-Path $repoRoot "ArduinoBridge"
if (-not (Test-Path -LiteralPath $arduinoSource)) {
    throw "Missing Arduino bridge folder: $arduinoSource"
}
Copy-Item -LiteralPath $arduinoSource -Destination (Join-Path $appDir "ArduinoBridge") -Recurse -Force

if (-not $UseInstaller -and -not $SourcePythonDir) {
    $SourcePythonDir = Get-AutoSourcePythonDir
}

if ($SourcePythonDir) {
    $sourcePythonExe = Join-Path $SourcePythonDir "python.exe"
    if (-not (Test-Path -LiteralPath $sourcePythonExe)) {
        throw "Source Python directory does not contain python.exe: $SourcePythonDir"
    }

    Write-Host ""
    Write-Host "Copying portable Python runtime from local Python: $SourcePythonDir"
    & robocopy $SourcePythonDir $pythonDir /E `
        /XD "__pycache__" "site-packages" "test" "tests" "Doc" "Tools" "Scripts" `
        /XF "*.pyc" "*.pyo"
    $robocopyExit = $LASTEXITCODE
    if ($robocopyExit -gt 7) {
        throw "robocopy failed with exit code $robocopyExit"
    }
    $global:LASTEXITCODE = 0

    $destinationSitePackages = Join-Path $pythonDir "Lib\site-packages"
    $pyserialCopied = $false
    $pillowCopied = $false
    $candidateSitePackages = @(
        (Join-Path $repoRoot ".venv\Lib\site-packages"),
        (Join-Path $SourcePythonDir "Lib\site-packages")
    )
    foreach ($candidateSitePackagesPath in $candidateSitePackages) {
        if (Test-Path -LiteralPath $candidateSitePackagesPath) {
            if (-not $pyserialCopied) {
                $pyserialCopied = Copy-PythonPackageFromSitePackages `
                    -SourceSitePackages $candidateSitePackagesPath `
                    -DestinationSitePackages $destinationSitePackages `
                    -PackageDirectory "serial" `
                    -DistInfoPattern "pyserial-*.dist-info"
            }
            if (-not $pillowCopied) {
                $pillowCopied = Copy-PythonPackageFromSitePackages `
                    -SourceSitePackages $candidateSitePackagesPath `
                    -DestinationSitePackages $destinationSitePackages `
                    -PackageDirectory "PIL" `
                    -DistInfoPattern "pillow-*.dist-info"
            }
        }
    }

    if (-not $pyserialCopied) {
        throw "pyserial was not found in local site-packages. Run Setup_CaptureBridge_Hub.bat or build with -UseInstaller."
    }
    if (-not $pillowCopied) {
        throw "Pillow was not found in local site-packages. Run Setup_CaptureBridge_Hub.bat or build with -UseInstaller."
    }
} elseif (-not $PythonInstallerPath) {
    $PythonInstallerPath = Join-Path $downloadsDir "python-$PythonVersion-amd64.exe"
    if ($Force -and (Test-Path -LiteralPath $PythonInstallerPath)) {
        Remove-Item -LiteralPath $PythonInstallerPath -Force
    }
    if (-not (Test-Path -LiteralPath $PythonInstallerPath)) {
        $pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        Write-Host ""
        Write-Host "Downloading CPython installer: $pythonUrl"
        Download-RequiredFile -Uri $pythonUrl -Destination $PythonInstallerPath
    }
}

if (-not $SourcePythonDir) {
    if (-not (Test-Path -LiteralPath $PythonInstallerPath)) {
        throw "Python installer not found: $PythonInstallerPath"
    }

    Write-Host ""
    Write-Host "Installing portable Python runtime into staging folder..."
    $installerArgs = @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=$pythonDir",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "PrependPath=0",
        "Include_test=0",
        "Include_doc=0",
        "Include_pip=1",
        "Include_tcltk=1"
    )
    & $PythonInstallerPath @installerArgs
    $installExit = $LASTEXITCODE
    if (($installExit -ne 0) -and ($installExit -ne 3010)) {
        throw "Python installer failed with exit code $installExit"
    }

    $pythonExe = Join-Path $pythonDir "python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Portable Python was not installed at $pythonExe"
    }

    Write-Host ""
    Write-Host "Installing offline runtime dependencies into bundled Python..."
    $oldNoUserSite = $env:PYTHONNOUSERSITE
    $env:PYTHONNOUSERSITE = "1"
    try {
        & $pythonExe -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $repoRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed with exit code $LASTEXITCODE"
        }
    } finally {
        $env:PYTHONNOUSERSITE = $oldNoUserSite
    }
}

$pythonExe = Join-Path $pythonDir "python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Portable Python was not prepared at $pythonExe"
}

Write-Host ""
Write-Host "Verifying bundled Python runtime..."
& $pythonExe -c "import tkinter, serial; from PIL import Image, ImageTk; print('runtime ok')"
if ($LASTEXITCODE -ne 0) {
    throw "Bundled Python runtime verification failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Removing Python caches from package..."
Get-ChildItem -LiteralPath $stageRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stageRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like "*.pyc") -or ($_.Name -like "*.pyo") } |
    Remove-Item -Force

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Write-Host ""
Write-Host "Creating ZIP: $zipPath"
Compress-Archive -Path (Join-Path $stageRoot "*") -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Done: $zipPath"
