param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$PythonVersion = "3.12.10"
$PackageUrl = "https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg"
$ExpectedSha512 = "BBDA4DCF688A94211B62D50968A91B38F305D0B8D1ECD90269F74A86F8A0A4FCEBB7CA162A0753A47691EB3DF0C964009BD3D8194C6FD19AFAE8D5FD01E1CC0F"

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$RuntimeBase = Join-Path $ProjectRoot ".runtime"
$RuntimeDir = Join-Path $RuntimeBase "python"
$RuntimePython = Join-Path $RuntimeDir "tools\python.exe"
$PackagePath = Join-Path $RuntimeBase "python-$PythonVersion.nupkg"
$PartialPath = "$PackagePath.download"
$StagingDir = Join-Path $RuntimeBase "python-staging-$PID"

function Test-PackageHash {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA512).Hash
    return $Actual -eq $ExpectedSha512
}

function Test-Runtime {
    if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
        return $false
    }
    try {
        & $RuntimePython -I -c "import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 10) else 1)"
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Karaoke Forge requires 64-bit Windows."
    }

    if (Test-Runtime) {
        Write-Host "Private Python $PythonVersion is ready."
        exit 0
    }

    New-Item -ItemType Directory -Path $RuntimeBase -Force | Out-Null

    if ((Test-Path -LiteralPath $PackagePath) -and -not (Test-PackageHash $PackagePath)) {
        Write-Host "Removing an invalid cached Python package."
        Remove-Item -LiteralPath $PackagePath -Force
    }

    if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
        Write-Host "Downloading private Python $PythonVersion runtime (about 14 MB)..."
        if (Test-Path -LiteralPath $PartialPath) {
            Remove-Item -LiteralPath $PartialPath -Force
        }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $PackageUrl -OutFile $PartialPath
        if (-not (Test-PackageHash $PartialPath)) {
            throw "The downloaded Python package failed its SHA-512 integrity check."
        }
        Move-Item -LiteralPath $PartialPath -Destination $PackagePath
    }

    if (Test-Path -LiteralPath $StagingDir) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $StagingDir | Out-Null

    Write-Host "Extracting the private Python runtime..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($PackagePath, $StagingDir)
    $StagingPython = Join-Path $StagingDir "tools\python.exe"
    if (-not (Test-Path -LiteralPath $StagingPython -PathType Leaf)) {
        throw "The Python package did not contain tools\python.exe."
    }
    & $StagingPython -I -c "import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 10) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "The extracted Python runtime could not be started."
    }

    if (Test-Path -LiteralPath $RuntimeDir) {
        Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
    }
    Move-Item -LiteralPath $StagingDir -Destination $RuntimeDir
    Write-Host "Private Python $PythonVersion is ready."
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    if (Test-Path -LiteralPath $PartialPath) {
        Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $StagingDir) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
