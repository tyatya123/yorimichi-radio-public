param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$DataDir = ''
)
$ErrorActionPreference = 'Stop'
$radioBase = 'http://127.0.0.1:' + $Port
$radioData = if ($DataDir) { [System.IO.Path]::GetFullPath($DataDir) } else { Join-Path $PSScriptRoot 'data' }
$radioRuntimeControl = Join-Path $PSScriptRoot 'runtime-control.ps1'
if (Test-Path -LiteralPath $radioRuntimeControl -PathType Leaf) { . $radioRuntimeControl }

function Get-RadioStopHealth {
    try { return (Invoke-RestMethod -Uri ($radioBase + '/api/health') -TimeoutSec 3) }
    catch { return $null }
}

$health = Get-RadioStopHealth
if (-not $health) {
    Write-Host ('AI Radio is not running on port ' + $Port + '.')
    if (Get-Command Stop-RadioManagedRuntimes -ErrorAction SilentlyContinue) {
        Stop-RadioManagedRuntimes -RadioPort $Port -RemoveLease
    }
    exit 0
}
if ($health.app -ne 'ai-radio') {
    throw 'Another application is using this port. It will not be stopped.'
}
if (-not $health.auth_instance) {
    throw 'An older AI Radio server is running. Stop it with its original launcher. This release will not send an unauthenticated shutdown request.'
}

$radioPython = $null
$radioPrefix = @()
foreach ($candidate in @((Join-Path $PSScriptRoot 'runtime\python\python.exe'), 'python.exe', 'py.exe', 'python3.exe')) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $prefix = @()
    if ($candidate -eq 'py.exe') { $prefix = @('-3') }
    try {
        $version = & $command.Source @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and ([version]($version | Select-Object -Last 1)) -ge [version]'3.11') {
            $radioPython = $command.Source
            $radioPrefix = $prefix
            break
        }
    } catch { }
}
if (-not $radioPython) {
    throw 'Python 3.11 or newer is required to authenticate this stop request. Run setup.cmd or use the Python installation that started the radio.'
}
$radioAuthHelper = Join-Path $PSScriptRoot 'local_auth.py'
if (-not (Test-Path -LiteralPath $radioAuthHelper -PathType Leaf)) {
    throw 'local_auth.py is missing. No unauthenticated stop request was sent.'
}
& $radioPython @radioPrefix $radioAuthHelper --port $Port --data-dir $radioData --shutdown
if ($LASTEXITCODE -ne 0) {
    throw 'The authenticated stop request failed. Use the same app folder and DataDir that started this radio. Local engines were left running.'
}
for ($radioAttempt = 0; $radioAttempt -lt 20; $radioAttempt++) {
    Start-Sleep -Milliseconds 150
    $remaining = Get-RadioStopHealth
    if (-not $remaining -or $remaining.app -ne 'ai-radio') { break }
}
if ($remaining -and $remaining.app -eq 'ai-radio') {
    throw 'The radio is still running. Local engines will not be stopped.'
}
if (Get-Command Stop-RadioManagedRuntimes -ErrorAction SilentlyContinue) {
    Stop-RadioManagedRuntimes -RadioPort $Port -RemoveLease
}
Write-Host 'AI Radio has been asked to stop. You can close the browser tab.'
