param(
    [switch]$PythonOnly,
    [switch]$VerifyOnly
)

# Windows PowerShell 5.1 compatible. Keep this source ASCII so the default
# Windows PowerShell script encoding cannot corrupt string literals.
$ErrorActionPreference = 'Stop'
$script:SetupRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$script:SetupRuntime = Join-Path $script:SetupRoot 'runtime'
$script:SetupStages = Join-Path $script:SetupRuntime '.setup'
$script:SetupDownloads = Join-Path $script:SetupRuntime '.downloads'
$script:SetupUtf8 = New-Object System.Text.UTF8Encoding($false)

function Read-SetupJson([string]$Path) {
    return ([System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json)
}

function Write-SetupJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [System.IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $Value -Depth 12), $script:SetupUtf8)
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-SetupDigest([string]$Value) {
    $digest = $Value.Trim().ToLowerInvariant() -replace '^sha256:', ''
    if ($digest -notmatch '^[0-9a-f]{64}$') { throw 'The runtime manifest contains an invalid SHA256 digest.' }
    return $digest
}

function Get-SetupFileDigest([string]$Path) {
    # Use .NET directly so child Windows PowerShell processes do not depend on
    # a PSModulePath inherited from PowerShell 7 or another host application.
    $stream = [System.IO.File]::OpenRead($Path)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}

function Assert-SetupChild([string]$Path, [string]$Parent) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ('Unsafe setup path rejected: ' + $resolved)
    }
    $ancestor = $resolved
    while ($ancestor -and $ancestor.Length -ge $script:SetupRoot.Length) {
        if (Test-Path -LiteralPath $ancestor) {
            if (([System.IO.File]::GetAttributes($ancestor) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw ('Setup does not follow runtime junctions or symbolic links: ' + $ancestor)
            }
        }
        if ([string]::Equals($ancestor, $script:SetupRoot, [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $ancestor = [System.IO.Path]::GetDirectoryName($ancestor)
    }
    return $resolved
}

function Get-SetupRelativePath([string]$Relative) {
    if (-not $Relative -or [System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':') -or $Relative -match '(^|[\\/])\.\.([\\/]|$)') {
        throw 'The runtime manifest contains an unsafe relative path.'
    }
    return (Assert-SetupChild -Path (Join-Path $script:SetupRoot $Relative) -Parent $script:SetupRoot)
}

function Assert-SetupSource([string]$Url, [string]$Label) {
    $uri = $null
    if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or $uri.UserInfo) {
        throw ('The ' + $Label + ' download must use an official HTTPS URL.')
    }
    $allowed = $false
    switch ($Label) {
        'python' { $allowed = ($uri.Host -eq 'www.python.org' -and $uri.AbsolutePath.StartsWith('/ftp/python/')) }
        'extractor' { $allowed = ($uri.Host -eq 'github.com' -and $uri.AbsolutePath.StartsWith('/ip7z/7zip/releases/download/')) }
        'voicevox' { $allowed = ($uri.Host -eq 'github.com' -and $uri.AbsolutePath.StartsWith('/VOICEVOX/voicevox_engine/releases/download/')) }
        'ollama' { $allowed = ($uri.Host -eq 'github.com' -and $uri.AbsolutePath.StartsWith('/ollama/ollama/releases/download/')) }
    }
    if (-not $allowed) { throw ('The ' + $Label + ' download is not an allowed official source.') }
}

function Get-SetupSpec([string]$Label) {
    $entry = $script:SetupManifest.$Label
    if (-not $entry) { throw ('Missing runtime manifest section: ' + $Label) }
    $expected = @{
        python = @{ destination = 'runtime/python'; executable = 'runtime/python/python.exe' }
        voicevox = @{ destination = 'runtime/voicevox'; executable = 'runtime/voicevox/windows-cpu/run.exe' }
        ollama = @{ destination = 'runtime/ollama'; executable = 'runtime/ollama/ollama.exe' }
    }
    if ($entry.destination -ne $expected[$Label].destination) { throw ('Unexpected install destination for ' + $Label) }
    $executable = if ($Label -eq 'python') { $expected[$Label].executable } else { [string]$entry.executable }
    if ($executable -ne $expected[$Label].executable) { throw ('Unexpected executable path for ' + $Label) }
    $archive = [string]$entry.archive
    if (-not $archive -or [System.IO.Path]::GetFileName($archive) -ne $archive -or $archive.Contains(':')) { throw 'Unsafe archive filename.' }
    Assert-SetupSource -Url ([string]$entry.url) -Label $Label
    return @{
        Label = $Label; Version = [string]$entry.version; Url = [string]$entry.url
        Digest = Get-SetupDigest ([string]$entry.sha256); Archive = $archive
        Destination = Get-SetupRelativePath ([string]$entry.destination)
        Executable = Get-SetupRelativePath $executable
        Entry = $entry
    }
}

function Test-SetupComponent($Spec) {
    if (-not (Test-Path -LiteralPath $Spec.Executable -PathType Leaf)) { return $false }
    $stamp = Join-Path $Spec.Destination '.ai-radio-install.json'
    if (-not (Test-Path -LiteralPath $stamp -PathType Leaf)) { return $false }
    try {
        $record = Read-SetupJson $stamp
        if ((Get-SetupDigest ([string]$record.archive_sha256)) -ne $Spec.Digest -or [string]$record.version -ne $Spec.Version) { return $false }
        $actual = Get-SetupFileDigest $Spec.Executable
        if ((Get-SetupDigest ([string]$record.executable_sha256)) -ne $actual) { return $false }
        if ($Spec.Label -eq 'python') {
            if (-not (Test-Path -LiteralPath (Join-Path $Spec.Destination ([string]$Spec.Entry.python_path_file)) -PathType Leaf)) { return $false }
            if (-not (Test-Path -LiteralPath (Join-Path $Spec.Destination (([string]$Spec.Entry.python_path_file) -replace '\._pth$', '.zip')) -PathType Leaf)) { return $false }
        }
        return $true
    } catch { return $false }
}

function Get-SetupDownload([string]$Url, [string]$Digest, [string]$Filename, [string]$Label) {
    Assert-SetupSource -Url $Url -Label $Label
    $digest = Get-SetupDigest $Digest
    if ([System.IO.Path]::GetFileName($Filename) -ne $Filename -or $Filename.Contains(':')) { throw 'Unsafe download filename.' }
    $folder = Join-Path $script:SetupDownloads $digest.Substring(0, 16)
    $target = Assert-SetupChild -Path (Join-Path $folder $Filename) -Parent $script:SetupDownloads
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        if ((Get-SetupFileDigest $target) -eq $digest) {
            Write-Host ('[' + $Label + '] Reusing the verified download.')
            return $target
        }
        Remove-Item -LiteralPath $target -Force
    }
    $partial = Assert-SetupChild -Path ($target + '.download') -Parent $script:SetupDownloads
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    Write-Host ('[' + $Label + '] Downloading: ' + $Url)
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(90)
    $cancel = New-Object System.Threading.CancellationTokenSource
    $cancel.CancelAfter([TimeSpan]::FromMinutes(90))
    $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $Url)
    $response = $null; $inputStream = $null; $outputStream = $null
    try {
        $client.DefaultRequestHeaders.UserAgent.ParseAdd('AI-Radio-Setup/' + $script:SetupManifest.app_version)
        $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
        $length = $response.Content.Headers.ContentLength
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [System.IO.File]::Open($partial, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $buffer = New-Object byte[] (256KB)
        [long]$received = 0
        $progressWatch = [System.Diagnostics.Stopwatch]::StartNew()
        while (($count = $inputStream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()) -gt 0) {
            $outputStream.Write($buffer, 0, $count)
            $received += $count
            if ($progressWatch.Elapsed.TotalSeconds -ge 2) {
                $percent = if ($length -gt 0) { [Math]::Min(100, [int](100 * $received / $length)) } else { -1 }
                Write-Progress -Id 71 -Activity ('Downloading ' + $Label) -Status (('{0:N0} MiB received' -f ($received / 1MB))) -PercentComplete $percent
                $progressWatch.Restart()
            }
        }
        $outputStream.Dispose(); $outputStream = $null
        if ($length -gt 0 -and $received -ne $length) { throw 'The download ended before all bytes were received.' }
        Write-Host ('[' + $Label + '] Checking SHA256...')
        if ((Get-SetupFileDigest $partial) -ne $digest) {
            throw ('SHA256 mismatch for ' + $Filename + '. No downloaded code was executed.')
        }
        Move-Item -LiteralPath $partial -Destination $target
        return $target
    } catch {
        if ($outputStream) { $outputStream.Dispose(); $outputStream = $null }
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        throw
    } finally {
        Write-Progress -Id 71 -Activity ('Downloading ' + $Label) -Completed
        if ($outputStream) { $outputStream.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Dispose(); $client.Dispose(); $cancel.Dispose()
    }
}

function Get-SetupArchiveTarget([string]$Stage, [string]$Entry) {
    if (-not $Entry -or [System.IO.Path]::IsPathRooted($Entry) -or $Entry.Contains(':') -or $Entry -match '(^|[\\/])\.\.([\\/]|$)') {
        throw ('Unsafe archive entry rejected: ' + $Entry)
    }
    return (Assert-SetupChild -Path (Join-Path $Stage $Entry) -Parent $Stage)
}

function Expand-SetupZip([string]$Archive, [string]$Stage) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        # Validate every target before writing any archive entries.
        foreach ($entry in $zip.Entries) {
            $null = Get-SetupArchiveTarget -Stage $Stage -Entry $entry.FullName
            $unixType = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($unixType -eq 0xA000 -or ($entry.ExternalAttributes -band 0x400) -ne 0) { throw 'Archive links are not accepted.' }
        }
        foreach ($entry in $zip.Entries) {
            $target = Get-SetupArchiveTarget -Stage $Stage -Entry $entry.FullName
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
            } else {
                New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($target)) -Force | Out-Null
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
            }
        }
    } finally { $zip.Dispose() }
}

function Expand-Setup7z([string]$Archive, [string]$Stage, [string]$Extractor) {
    Write-Host '[voicevox] Checking archive paths...'
    $listing = @(& $Extractor 'l' '-slt' $Archive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the VOICEVOX archive.' }
    $entries = $false; $count = 0
    foreach ($item in $listing) {
        $line = [string]$item
        if ($line -match '^-{10,}$') { $entries = $true; continue }
        if (-not $entries) { continue }
        if ($line -match '^(Symbolic Link|Hard Link|Reparse|Alternate Stream)\s*=') { throw 'Archive links and alternate streams are not accepted.' }
        if ($line.StartsWith('Path = ')) {
            $null = Get-SetupArchiveTarget -Stage $Stage -Entry $line.Substring(7)
            $count++
        }
    }
    if ($count -eq 0) { throw 'The VOICEVOX archive did not contain a validated file listing.' }
    Write-Host '[voicevox] Extracting the CPU engine. This may take a few minutes...'
    & $Extractor 'x' '-y' '-bd' '-bso0' '-bsp0' ('-o' + $Stage) $Archive
    if ($LASTEXITCODE -ne 0) { throw 'VOICEVOX extraction failed. No existing runtime was replaced.' }
}

function Assert-SetupTree([string]$Stage) {
    $queue = New-Object 'System.Collections.Generic.Queue[string]'
    $queue.Enqueue($Stage)
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($path in [System.IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $resolved = Assert-SetupChild -Path $path -Parent $Stage
            $attributes = [System.IO.File]::GetAttributes($resolved)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Extracted links and reparse points are not accepted.' }
            if (($attributes -band [System.IO.FileAttributes]::Directory) -ne 0) { $queue.Enqueue($resolved) }
        }
    }
}

function Install-SetupComponent($Spec, [string]$Extractor = '') {
    if (Test-SetupComponent $Spec) {
        Write-Host ('[' + $Spec.Label + '] Already installed and verified; leaving it unchanged.')
        return
    }
    if (Test-Path -LiteralPath $Spec.Destination) {
        # Do not replace manually installed, modified, or unrelated files.
        if (@(Get-ChildItem -LiteralPath $Spec.Destination -Force).Count -gt 0) {
            throw ('Existing unverified files were preserved at ' + $Spec.Destination + '. Install this release in a fresh folder, or keep using the existing runtime.')
        }
        $null = Assert-SetupChild -Path $Spec.Destination -Parent $script:SetupRuntime
        Remove-Item -LiteralPath $Spec.Destination -Force
    }
    $archive = Get-SetupDownload -Url $Spec.Url -Digest $Spec.Digest -Filename $Spec.Archive -Label $Spec.Label
    $stage = Assert-SetupChild -Path (Join-Path $script:SetupStages ($Spec.Label + '-' + [Guid]::NewGuid().ToString('N'))) -Parent $script:SetupStages
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    try {
        Write-Host ('[' + $Spec.Label + '] Preparing portable files...')
        if ($Spec.Label -eq 'voicevox') { Expand-Setup7z -Archive $archive -Stage $stage -Extractor $Extractor }
        else { Expand-SetupZip -Archive $archive -Stage $stage }
        Assert-SetupTree $stage
        $relativeExe = $Spec.Executable.Substring($Spec.Destination.TrimEnd('\', '/').Length + 1)
        $stageExe = Get-SetupArchiveTarget -Stage $stage -Entry $relativeExe
        if (-not (Test-Path -LiteralPath $stageExe -PathType Leaf)) { throw ('The expected executable was missing from the ' + $Spec.Label + ' archive.') }
        if ($Spec.Label -eq 'python') {
            $pathFile = [string]$Spec.Entry.python_path_file
            if ($pathFile -notmatch '^python[0-9]+\._pth$') { throw 'Invalid embedded Python path filename.' }
            $zipName = $pathFile -replace '\._pth$', '.zip'
            if (-not (Test-Path -LiteralPath (Join-Path $stage $zipName) -PathType Leaf)) { throw 'The Python standard-library ZIP is missing.' }
            [System.IO.File]::WriteAllText((Join-Path $stage $pathFile), ($zipName + "`r`n.`r`n..\..`r`n# Standard library and this application only; no global site-packages.`r`n"), $script:SetupUtf8)
        }
        $record = @{
            schema = 1; component = $Spec.Label; version = $Spec.Version
            source = $Spec.Url; archive_sha256 = $Spec.Digest
            executable_sha256 = Get-SetupFileDigest $stageExe
            installed_utc = [DateTime]::UtcNow.ToString('o')
        }
        Write-SetupJson -Path (Join-Path $stage '.ai-radio-install.json') -Value $record
        $null = Assert-SetupChild -Path $Spec.Destination -Parent $script:SetupRuntime
        New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($Spec.Destination)) -Force | Out-Null
        Move-Item -LiteralPath $stage -Destination $Spec.Destination
        Write-Host ('[' + $Spec.Label + '] Installed ' + $Spec.Version + '.')
        try {
            $verifiedArchive = Assert-SetupChild -Path $archive -Parent $script:SetupDownloads
            Remove-Item -LiteralPath $verifiedArchive -Force
        } catch {
            Write-Warning ('The installed runtime is ready, but its download cache could not be removed: ' + $_.Exception.Message)
        }
    } finally {
        if (Test-Path -LiteralPath $stage) {
            $null = Assert-SetupChild -Path $stage -Parent $script:SetupStages
            Remove-Item -LiteralPath $stage -Recurse -Force
        }
    }
}

function Initialize-SetupSettings {
    $dataDir = Join-Path $script:SetupRoot 'data'
    $settingsFile = Join-Path $dataDir 'settings.json'
    if (Test-Path -LiteralPath $settingsFile) {
        Write-Host '[settings] Existing settings are preserved without changes.'
        return $dataDir
    }
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $settings = ($script:SetupManifest.settings | ConvertTo-Json -Depth 12 | ConvertFrom-Json)
    if ($PythonOnly) { $settings.llm_enabled = $false }
    # File.Move without an overwrite option is atomic on this same volume and
    # also preserves settings created concurrently by a separately started app.
    $temporary = Join-Path $dataDir ('settings-setup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $settings -Depth 12), $script:SetupUtf8)
        try {
            [System.IO.File]::Move($temporary, $settingsFile)
            Write-Host '[settings] Created the recommended first-run profile.'
        } catch [System.IO.IOException] {
            if (-not (Test-Path -LiteralPath $settingsFile -PathType Leaf)) { throw }
            Write-Host '[settings] Settings created by another app process were preserved.'
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
    return $dataDir
}

function Get-SetupModelManifestPath {
    $name = [string]$script:SetupManifest.model.name
    if ($name -notmatch '^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$') { throw 'The pinned Ollama model name is invalid.' }
    $parts = $name.Split(':')
    $repository = if ($parts[0].Contains('/')) { $parts[0] } else { 'library/' + $parts[0] }
    return (Get-SetupRelativePath ('runtime/models/manifests/registry.ollama.ai/' + $repository + '/' + $parts[1]))
}

function Find-SetupModel {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 8
    foreach ($item in @($tags.models)) {
        if ([string]$item.name -eq [string]$script:SetupManifest.model.name -or [string]$item.model -eq [string]$script:SetupManifest.model.name) { return $item }
    }
    return $null
}

function Test-SetupOwnedOllama {
    $owners = Read-RadioRuntimeOwners
    if (-not $owners.ContainsKey('ollama')) { return $false }
    return [bool](Get-RadioOwnedProcess -Record $owners['ollama'] -Role 'ollama')
}

function Pull-SetupModel {
    if (-not (Test-SetupOwnedOllama)) {
        throw 'Another Ollama service is using port 11434. Its models were not changed. Exit that service from its tray icon, then run setup.cmd again to use this app-owned service. You can also use setup.cmd -PythonOnly.'
    }
    $name = [string]$script:SetupManifest.model.name
    Write-Host ('[model] Downloading ' + $name + ' into runtime/models (several GB)...')
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(90)
    $request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, 'http://127.0.0.1:11434/api/pull')
    $body = ConvertTo-Json -Compress -InputObject @{model = $name; stream = $true}
    $request.Content = New-Object System.Net.Http.StringContent($body, [System.Text.Encoding]::UTF8, 'application/json')
    $response = $null; $reader = $null; $stream = $null
    try {
        $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $previous = ''; $success = $false
        while ($true) {
            $read = $reader.ReadLineAsync()
            if (-not $read.Wait(120000)) { throw 'The model download stopped responding. Run setup.cmd again to resume the Ollama pull.' }
            $line = $read.GetAwaiter().GetResult()
            if ($null -eq $line) { break }
            if (-not $line.Trim()) { continue }
            $message = $line | ConvertFrom-Json
            if ($message.error) { throw ([string]$message.error) }
            $status = [string]$message.status
            if ($status -eq 'success') { $success = $true }
            if ($watch.Elapsed.TotalSeconds -ge 1 -or $status -ne $previous) {
                $percent = if ($message.total -gt 0) { [Math]::Min(100, [int](100 * [double]$message.completed / [double]$message.total)) } else { -1 }
                Write-Progress -Id 72 -Activity ('Downloading model: ' + $name) -Status $status -PercentComplete $percent
                $previous = $status; $watch.Restart()
            }
        }
        if (-not $success) { throw 'The model download did not report success. Run setup.cmd again to resume it.' }
    } finally {
        Write-Progress -Id 72 -Activity ('Downloading model: ' + $name) -Completed
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Dispose(); $client.Dispose()
    }
}

function Confirm-SetupModel {
    $expected = Get-SetupDigest ([string]$script:SetupManifest.model.digest)
    $model = Find-SetupModel
    if (-not $model) {
        Pull-SetupModel
        $model = Find-SetupModel
    }
    if (-not $model) { throw 'The selected model was not found after the pull.' }
    if ((Get-SetupDigest ([string]$model.digest)) -ne $expected) {
        throw 'The model tag differs from the pinned SHA256 digest. Existing models were preserved; this setup will not silently use a different model.'
    }
    $manifestFile = Get-SetupModelManifestPath
    if (Test-Path -LiteralPath $manifestFile -PathType Leaf) {
        if ((Get-SetupFileDigest $manifestFile) -ne $expected) { throw 'The dedicated model manifest failed SHA256 verification.' }
        Write-Host '[model] The pinned local model is ready and verified.'
    } elseif (Test-SetupOwnedOllama) {
        throw 'The model API responded, but the expected manifest is missing from runtime/models.'
    } else {
        Write-Host '[model] Using the matching model already available in the existing Ollama service. Its files were not changed.'
    }
}

function Test-SetupOffline {
    $missing = @()
    $components = if ($PythonOnly) { @('python') } else { @('python', 'voicevox', 'ollama') }
    foreach ($label in $components) {
        $spec = Get-SetupSpec $label
        if (Test-Path -LiteralPath $spec.Executable -PathType Leaf) { Write-Host ('[OK] ' + $spec.Executable) }
        else { $missing += $label; Write-Host ('[MISSING] ' + $spec.Executable) }
    }
    if (-not $PythonOnly) {
        $modelFile = Get-SetupModelManifestPath
        if (-not (Test-Path -LiteralPath $modelFile -PathType Leaf)) {
            $missing += 'model manifest'; Write-Host ('[MISSING] ' + $modelFile)
        } elseif ((Get-SetupFileDigest $modelFile) -ne (Get-SetupDigest ([string]$script:SetupManifest.model.digest))) {
            $missing += 'model digest'; Write-Host '[MISMATCH] The model manifest does not match the pinned digest.'
        } else { Write-Host '[OK] The dedicated model manifest matches its pinned SHA256.' }
    }
    if ($missing.Count -gt 0) { throw ('Offline verification found missing or mismatched components: ' + ($missing -join ', ')) }
    Write-Host 'Offline verification passed. No network requests or engine startup were performed.'
}

$setupMutex = $null; $setupAcquired = $false
try {
    $manifestPath = Join-Path $script:SetupRoot 'runtime-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'runtime-manifest.json is missing. Extract the complete app ZIP before running setup.' }
    $script:SetupManifest = Read-SetupJson $manifestPath
    if ($script:SetupManifest.schema -ne 1) { throw 'Unsupported runtime manifest schema.' }
    Write-Host ('AI Radio ' + $script:SetupManifest.app_version + ' - portable Windows x64 setup')
    if ($VerifyOnly) { Test-SetupOffline; exit 0 }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($architecture -ne 'AMD64') { throw 'This package contains x64 runtimes. ARM64 and 32-bit Windows are not supported by this setup.' }
    Write-Host 'Official third-party downloads will be installed on this PC; no binaries or model weights are bundled in the app ZIP.'
    Write-Host 'Sources and license information:'
    Write-Host '  Python:  https://www.python.org/downloads/windows/'
    Write-Host '  VOICEVOX: https://github.com/VOICEVOX/voicevox_engine'
    Write-Host '  VOICEVOX voice terms: https://www.meimeihimari.com/terms-of-use'
    Write-Host '  Ollama:  https://github.com/ollama/ollama'
    Write-Host ('  Model:   ' + $script:SetupManifest.model.source)
    Write-Host '  Extractor: https://github.com/ip7z/7zip'
    Write-Host 'Existing music files and data/settings.json will be preserved.'
    if ($PythonOnly) { Write-Host 'Basic mode: Python only. New settings will use browser speech and no LLM.' }
    else { Write-Host 'Full mode downloads several GB and needs additional space for extraction and models.' }

    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { $lockDigest = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:SetupRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 24) }
    finally { $hasher.Dispose() }
    $setupMutex = New-Object System.Threading.Mutex($false, ('Local\AIRadioSetup-' + $lockDigest))
    try { $setupAcquired = $setupMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $setupAcquired = $true }
    if (-not $setupAcquired) { throw 'Another setup is already working in this folder. Wait for it to finish.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Add-Type -AssemblyName System.Net.Http
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $script:SetupRuntime -Force | Out-Null
    New-Item -ItemType Directory -Path $script:SetupStages -Force | Out-Null
    Install-SetupComponent -Spec (Get-SetupSpec 'python')
    if (-not $PythonOnly) {
        $voiceSpec = Get-SetupSpec 'voicevox'
        $extractor = ''
        if (-not (Test-SetupComponent $voiceSpec)) {
            $tool = $script:SetupManifest.extractor
            $extractor = Get-SetupDownload -Url ([string]$tool.url) -Digest ([string]$tool.sha256) -Filename ([string]$tool.file) -Label 'extractor'
        }
        Install-SetupComponent -Spec $voiceSpec -Extractor $extractor
        Install-SetupComponent -Spec (Get-SetupSpec 'ollama')
    }
    $setupData = Initialize-SetupSettings
    if (-not $PythonOnly) {
        $helper = Join-Path $script:SetupRoot 'runtime-control.ps1'
        if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'runtime-control.ps1 is missing.' }
        . $helper
        Start-RadioManagedRuntimes -DataDir $setupData
        if (-not (Test-RadioRuntimeHealth 'voicevox')) { throw 'VOICEVOX is not ready. See runtime/logs, resolve the engine error, and run setup.cmd again.' }
        if (-not (Test-RadioRuntimeHealth 'ollama')) { throw 'Ollama is not ready. See runtime/logs, resolve the engine error, and run setup.cmd again.' }
        $settingsFile = Join-Path $setupData 'settings.json'
        $existingSettings = Read-SetupJson $settingsFile
        if ($existingSettings.ollama_url -and ([string]$existingSettings.ollama_url).TrimEnd('/') -notin @('http://127.0.0.1:11434', 'http://localhost:11434')) {
            throw 'A custom Ollama endpoint is configured. Existing settings were preserved; automatic model installation only uses this app-owned local service at 127.0.0.1:11434.'
        }
        Confirm-SetupModel
    }
    Write-Host ''
    Write-Host 'Setup complete. Run start.cmd to open AI Radio.'
    Write-Host 'The radio was not started automatically. Use stop.cmd to stop app-owned engines.'
    exit 0
} catch {
    Write-Host ''
    Write-Host ('Setup did not complete: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Existing music and settings were kept. Correct the reported issue and run setup.cmd again.'
    exit 1
} finally {
    if ($setupMutex) {
        if ($setupAcquired) { try { $setupMutex.ReleaseMutex() } catch { } }
        $setupMutex.Dispose()
    }
}
