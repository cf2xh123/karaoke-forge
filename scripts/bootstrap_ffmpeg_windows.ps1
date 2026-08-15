param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$FfmpegVersion = "8.1.2"
$ArchiveName = "ffmpeg-$FfmpegVersion-essentials_build.zip"
$PrimaryUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/$ArchiveName"
$FallbackUrl = "https://www.gyan.dev/ffmpeg/builds/packages/$ArchiveName"
$ExpectedSha256 = "DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC"
$MaximumArchiveBytes = 512MB
$MaximumEntryBytes = 1GB
$MaximumExpandedBytes = 2GB
$MaximumEntryCount = 20000
$MaximumCompressionRatio = 250

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$RuntimeBase = Join-Path $ProjectRoot ".runtime"
$RuntimeDir = Join-Path $RuntimeBase "ffmpeg"
$ArchivePath = Join-Path $RuntimeBase $ArchiveName
$OperationId = "$PID-$([Guid]::NewGuid().ToString('N'))"
$PartialPath = Join-Path $RuntimeBase "$ArchiveName.download-$OperationId"
$WorkDir = Join-Path $RuntimeBase "ffmpeg-staging-$OperationId"
$ExtractDir = Join-Path $WorkDir "extract"
$BackupDir = Join-Path $RuntimeBase "ffmpeg-backup-$OperationId"
$LockPath = Join-Path $RuntimeBase "ffmpeg-bootstrap.lock"
$LockStream = $null
$InstallSucceeded = $false

function Test-ArchiveHash {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $File = Get-Item -LiteralPath $Path
    if (($File.Length -le 0) -or ($File.Length -gt $MaximumArchiveBytes)) {
        return $false
    }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    return $Actual -eq $ExpectedSha256
}

function Test-VersionCommand {
    param(
        [string]$Executable,
        [string]$ToolName
    )

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Executable
    $StartInfo.Arguments = "-hide_banner -version"
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) {
            return $false
        }
        if (-not $Process.WaitForExit(15000)) {
            $Process.Kill()
            $Process.WaitForExit()
            return $false
        }
        $Output = $Process.StandardOutput.ReadToEnd() + $Process.StandardError.ReadToEnd()
        if ($Process.ExitCode -ne 0) {
            return $false
        }
        $ExpectedPrefix = "(?m)^$ToolName version $([Regex]::Escape($FfmpegVersion))(?:[- ]|$)"
        return $Output -match $ExpectedPrefix
    }
    catch {
        return $false
    }
    finally {
        $Process.Dispose()
    }
}

function Test-FfmpegRuntime {
    param([string]$Root)

    $Ffmpeg = Join-Path $Root "bin\ffmpeg.exe"
    $Ffprobe = Join-Path $Root "bin\ffprobe.exe"
    if (-not (Test-Path -LiteralPath $Ffmpeg -PathType Leaf)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $Ffprobe -PathType Leaf)) {
        return $false
    }
    return (Test-VersionCommand $Ffmpeg "ffmpeg") -and (Test-VersionCommand $Ffprobe "ffprobe")
}

function Enter-BootstrapLock {
    $Deadline = [DateTime]::UtcNow.AddMinutes(2)
    while ($true) {
        try {
            return [System.IO.File]::Open(
                $LockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $Deadline) {
                throw "Another FFmpeg setup is still running. Close it or wait, then try again."
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Save-HttpsFileLimited {
    param(
        [string]$Url,
        [string]$Destination,
        [Int64]$MaximumBytes
    )

    $Uri = [Uri]$Url
    if ($Uri.Scheme -ne "https") {
        throw "FFmpeg downloads must use HTTPS."
    }

    $Request = [System.Net.HttpWebRequest]::Create($Uri)
    $Request.Method = "GET"
    $Request.AllowAutoRedirect = $true
    $Request.MaximumAutomaticRedirections = 10
    $Request.Timeout = 60000
    $Request.ReadWriteTimeout = 60000
    $Request.UserAgent = "Karaoke-Forge/$FfmpegVersion"
    $Response = $null
    $InputStream = $null
    $OutputStream = $null
    try {
        $Response = [System.Net.HttpWebResponse]$Request.GetResponse()
        if ($Response.ResponseUri.Scheme -ne "https") {
            throw "FFmpeg download redirected away from HTTPS."
        }
        $StatusCode = [int]$Response.StatusCode
        if (($StatusCode -lt 200) -or ($StatusCode -ge 300)) {
            throw "FFmpeg server returned HTTP $StatusCode."
        }
        if (($Response.ContentLength -gt $MaximumBytes) -or ($Response.ContentLength -eq 0)) {
            throw "FFmpeg archive size is invalid."
        }

        $InputStream = $Response.GetResponseStream()
        $OutputStream = [System.IO.File]::Open(
            $Destination,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $Buffer = New-Object byte[] (1MB)
        [Int64]$DownloadedBytes = 0
        while ($true) {
            $Read = $InputStream.Read($Buffer, 0, $Buffer.Length)
            if ($Read -le 0) {
                break
            }
            $DownloadedBytes += $Read
            if ($DownloadedBytes -gt $MaximumBytes) {
                throw "FFmpeg archive exceeded the safe download size limit."
            }
            $OutputStream.Write($Buffer, 0, $Read)
        }
        if ($DownloadedBytes -le 0) {
            throw "FFmpeg server returned an empty archive."
        }
        $OutputStream.Flush()
    }
    finally {
        if ($OutputStream -ne $null) {
            $OutputStream.Dispose()
        }
        if ($InputStream -ne $null) {
            $InputStream.Dispose()
        }
        if ($Response -ne $null) {
            $Response.Dispose()
        }
    }
}

function Get-VerifiedArchive {
    if ((Test-Path -LiteralPath $ArchivePath) -and -not (Test-ArchiveHash $ArchivePath)) {
        Write-Host "Removing an invalid cached FFmpeg archive."
        Remove-Item -LiteralPath $ArchivePath -Force
    }
    if (Test-ArchiveHash $ArchivePath) {
        Write-Host "Using the verified cached FFmpeg archive."
        return
    }

    $DownloadErrors = New-Object System.Collections.Generic.List[string]
    foreach ($Url in @($PrimaryUrl, $FallbackUrl)) {
        if (Test-Path -LiteralPath $PartialPath) {
            Remove-Item -LiteralPath $PartialPath -Force
        }
        try {
            Write-Host "Downloading private FFmpeg $FfmpegVersion (about 110 MB; this can take several minutes)..."
            Write-Host "Source: $Url"
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Save-HttpsFileLimited -Url $Url -Destination $PartialPath -MaximumBytes $MaximumArchiveBytes
            if (-not (Test-ArchiveHash $PartialPath)) {
                throw "The download failed its SHA-256 integrity check."
            }
            Move-Item -LiteralPath $PartialPath -Destination $ArchivePath
            return
        }
        catch {
            $DownloadErrors.Add("$Url : $($_.Exception.Message)")
            if (Test-Path -LiteralPath $PartialPath) {
                Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
            }
        }
    }
    throw "FFmpeg could not be downloaded from either trusted source. $($DownloadErrors -join ' | ')"
}

function Test-ReservedDeviceName {
    param([string]$Segment)

    $BaseName = $Segment.Split('.')[0]
    return $BaseName -match '^(?i:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$'
}

function Expand-VerifiedArchive {
    param(
        [string]$Path,
        [string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $Destination | Out-Null
    $DestinationRoot = [System.IO.Path]::GetFullPath($Destination)
    $DestinationPrefix = $DestinationRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $SeenPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $ExpandedBytes = [Int64]0
    $EntryCount = 0
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($Entry in $Archive.Entries) {
            $EntryCount += 1
            if ($EntryCount -gt $MaximumEntryCount) {
                throw "The FFmpeg archive contains too many entries."
            }

            $EntryName = $Entry.FullName.Replace('\', '/')
            $IsDirectory = $EntryName.EndsWith('/')
            $RelativePath = $EntryName.TrimEnd('/')
            if ([string]::IsNullOrWhiteSpace($RelativePath)) {
                throw "The FFmpeg archive contains an empty path."
            }
            if (($EntryName.StartsWith('/')) -or ($EntryName.StartsWith('\'))) {
                throw "The FFmpeg archive contains an absolute path."
            }
            if ($RelativePath.Length -gt 1024) {
                throw "The FFmpeg archive contains an overlong path."
            }

            $Segments = $RelativePath.Split('/')
            foreach ($Segment in $Segments) {
                if ([string]::IsNullOrEmpty($Segment) -or ($Segment -eq '.') -or ($Segment -eq '..')) {
                    throw "The FFmpeg archive contains an unsafe path segment."
                }
                if (($Segment.Length -gt 255) -or $Segment.EndsWith(' ') -or $Segment.EndsWith('.')) {
                    throw "The FFmpeg archive contains an invalid Windows path segment."
                }
                if ($Segment.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) {
                    throw "The FFmpeg archive contains invalid filename characters."
                }
                if (Test-ReservedDeviceName $Segment) {
                    throw "The FFmpeg archive contains a reserved Windows device name."
                }
                if ($Segment -match '[\x00-\x1F\x7F]') {
                    throw "The FFmpeg archive contains control characters."
                }
            }

            if (-not $SeenPaths.Add($RelativePath)) {
                throw "The FFmpeg archive contains duplicate paths."
            }
            $UnixFileType = (($Entry.ExternalAttributes -shr 16) -band 0xF000)
            $WindowsAttributes = ($Entry.ExternalAttributes -band 0xFFFF)
            if (($UnixFileType -eq 0xA000) -or (($WindowsAttributes -band 0x400) -ne 0)) {
                throw "The FFmpeg archive contains a link or reparse point."
            }
            if (($Entry.Length -lt 0) -or ($Entry.Length -gt $MaximumEntryBytes)) {
                throw "The FFmpeg archive contains an oversized entry."
            }
            $ExpandedBytes += $Entry.Length
            if ($ExpandedBytes -gt $MaximumExpandedBytes) {
                throw "The FFmpeg archive expands beyond the allowed size."
            }
            if (($Entry.Length -gt 1MB) -and ($Entry.CompressedLength -le 0)) {
                throw "The FFmpeg archive contains an invalid compressed entry."
            }
            if (($Entry.CompressedLength -gt 0) -and (($Entry.Length / $Entry.CompressedLength) -gt $MaximumCompressionRatio)) {
                throw "The FFmpeg archive contains a suspicious compression ratio."
            }

            $PlatformRelativePath = $RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
            $TargetPath = [System.IO.Path]::GetFullPath((Join-Path $DestinationRoot $PlatformRelativePath))
            if (-not $TargetPath.StartsWith($DestinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "The FFmpeg archive attempts to escape the staging directory."
            }
            if ($IsDirectory) {
                New-Item -ItemType Directory -Path $TargetPath -Force | Out-Null
                continue
            }

            $ParentPath = Split-Path -Parent $TargetPath
            New-Item -ItemType Directory -Path $ParentPath -Force | Out-Null
            $InputStream = $Entry.Open()
            $OutputStream = [System.IO.File]::Open(
                $TargetPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            try {
                $InputStream.CopyTo($OutputStream)
            }
            finally {
                $OutputStream.Dispose()
                $InputStream.Dispose()
            }
        }
    }
    finally {
        $Archive.Dispose()
    }
    if ($EntryCount -eq 0) {
        throw "The FFmpeg archive is empty."
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Karaoke Forge requires 64-bit Windows."
    }
    $NativeArchitecture = if ($env:PROCESSOR_ARCHITEW6432) {
        $env:PROCESSOR_ARCHITEW6432
    }
    else {
        $env:PROCESSOR_ARCHITECTURE
    }
    if ($NativeArchitecture -ne "AMD64") {
        throw "This private FFmpeg package supports x64 Windows only."
    }
    New-Item -ItemType Directory -Path $RuntimeBase -Force | Out-Null
    $LockStream = Enter-BootstrapLock

    if (Test-FfmpegRuntime $RuntimeDir) {
        Write-Host "Private FFmpeg $FfmpegVersion is ready."
        exit 0
    }

    Get-VerifiedArchive
    if (Test-Path -LiteralPath $WorkDir) {
        Remove-Item -LiteralPath $WorkDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $WorkDir | Out-Null
    Write-Host "Safely extracting the private FFmpeg runtime..."
    Expand-VerifiedArchive -Path $ArchivePath -Destination $ExtractDir

    $FfmpegMatches = @(Get-ChildItem -LiteralPath $ExtractDir -Recurse -File -Filter "ffmpeg.exe" -Force)
    $FfprobeMatches = @(Get-ChildItem -LiteralPath $ExtractDir -Recurse -File -Filter "ffprobe.exe" -Force)
    if (($FfmpegMatches.Count -ne 1) -or ($FfprobeMatches.Count -ne 1)) {
        throw "The FFmpeg archive did not contain exactly one ffmpeg.exe and ffprobe.exe."
    }
    if ($FfmpegMatches[0].Directory.FullName -ne $FfprobeMatches[0].Directory.FullName) {
        throw "The FFmpeg tools were not found in the same directory."
    }
    if ($FfmpegMatches[0].Directory.Name -ne "bin") {
        throw "The FFmpeg tools were not stored in the expected bin directory."
    }
    $CandidateDir = $FfmpegMatches[0].Directory.Parent.FullName

    $PreviousMoved = $false
    try {
        if (Test-Path -LiteralPath $RuntimeDir) {
            Move-Item -LiteralPath $RuntimeDir -Destination $BackupDir
            $PreviousMoved = $true
        }
        Move-Item -LiteralPath $CandidateDir -Destination $RuntimeDir
        if (-not (Test-FfmpegRuntime $RuntimeDir)) {
            throw "The installed FFmpeg runtime could not be started."
        }
        $InstallSucceeded = $true
        if ($PreviousMoved -and (Test-Path -LiteralPath $BackupDir)) {
            Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    catch {
        if (Test-Path -LiteralPath $RuntimeDir) {
            Remove-Item -LiteralPath $RuntimeDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($PreviousMoved -and (Test-Path -LiteralPath $BackupDir)) {
            Move-Item -LiteralPath $BackupDir -Destination $RuntimeDir
        }
        throw
    }

    Write-Host "Private FFmpeg $FfmpegVersion is ready."
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
    if (Test-Path -LiteralPath $WorkDir) {
        Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($InstallSucceeded -and (Test-Path -LiteralPath $BackupDir)) {
        Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $LockStream) {
        $LockStream.Dispose()
    }
}
