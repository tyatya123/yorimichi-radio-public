param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [string]$DataDir = ''
)
$ErrorActionPreference = 'Stop'
$radioBase = 'http://127.0.0.1:' + $Port
$radioData = if ($DataDir) { [System.IO.Path]::GetFullPath($DataDir) } else { Join-Path $PSScriptRoot 'data' }

function Get-RadioHealth {
    try {
        $health = Invoke-RestMethod -Uri ($radioBase + '/api/health') -TimeoutSec 2
        if ($health.app -eq 'ai-radio') { return $health }
    } catch { }
    return $null
}

function Test-RadioPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if ($task.Wait(350)) { return $client.Connected }
        return $false
    } catch { return $false }
    finally { $client.Dispose() }
}

$radioHealth = Get-RadioHealth
$radioAlreadyRunning = [bool]$radioHealth
if ($radioAlreadyRunning -and -not $radioHealth.auth_instance) {
    throw 'An older AI Radio server is already running. Stop it with its original launcher, then run this start.cmd again. No unauthenticated shutdown will be attempted.'
}
if (-not $radioAlreadyRunning -and (Test-RadioPort)) {
    throw ('Port ' + $Port + ' is used by another application. Run: .\start.ps1 -Port 8766')
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
if (-not $radioPython -and (-not $radioAlreadyRunning -or -not $NoBrowser)) {
    throw 'Run setup.cmd first to prepare portable Python. Alternatively install Python 3.11 or newer from https://www.python.org/downloads/windows/ and enable Add Python to PATH.'
}

$radioAuthHelper = Join-Path $PSScriptRoot 'local_auth.py'
if (-not $NoBrowser -and -not (Test-Path -LiteralPath $radioAuthHelper -PathType Leaf)) {
    throw 'local_auth.py is missing. Extract the complete application ZIP, then run start.cmd again.'
}
function Open-RadioSession {
    & $radioPython @radioPrefix $radioAuthHelper --port $Port --data-dir $radioData --open-browser
    if ($LASTEXITCODE -ne 0) {
        throw 'The authenticated browser session could not be opened. Use the same app folder and DataDir that started this radio, or restart it with its original launcher.'
    }
}

New-Item -ItemType Directory -Path $radioData -Force | Out-Null
$radioRuntimeControl = Join-Path $PSScriptRoot 'runtime-control.ps1'
if (Test-Path -LiteralPath $radioRuntimeControl -PathType Leaf) {
    . $radioRuntimeControl
    try { Start-RadioManagedRuntimes -RadioPort $Port -DataDir $radioData -RegisterLease }
    catch { Write-Warning ('Local engines could not be prepared. The radio can use fallback mode. ' + $_.Exception.Message) }
}
if ($radioAlreadyRunning) {
    if (Get-Command Confirm-RadioRuntimeLease -ErrorAction SilentlyContinue) {
        $radioExistingPid = 0
        $radioPidPath = Join-Path $radioData 'server.pid'
        if (Test-Path -LiteralPath $radioPidPath -PathType Leaf) {
            try { $radioExistingPid = [int](Get-Content -LiteralPath $radioPidPath -Raw) } catch { }
        }
        try { Confirm-RadioRuntimeLease -RadioPort $Port -ProcessId $radioExistingPid }
        catch { Write-Warning ('Could not record the radio session: ' + $_.Exception.Message) }
    }
    Write-Host 'AI Radio is already running.'
    if (-not $NoBrowser) { Open-RadioSession }
    exit 0
}
$radioApp = Join-Path $PSScriptRoot 'app.py'
$radioArgs = @($radioPrefix) + @(('"' + $radioApp + '"'), '--port', [string]$Port, '--data-dir', ('"' + $radioData + '"'), '--no-browser')
$radioProcess = Start-Process -FilePath $radioPython -ArgumentList $radioArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $radioData 'server-stdout.log') -RedirectStandardError (Join-Path $radioData 'server-stderr.log')

for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $radioHealth = Get-RadioHealth
    if ($radioHealth) {
        if (-not $radioHealth.auth_instance) {
            throw 'A server without authenticated sessions answered on this port. Use its original launcher to stop it before restarting this release.'
        }
        if (Get-Command Confirm-RadioRuntimeLease -ErrorAction SilentlyContinue) {
            try { Confirm-RadioRuntimeLease -RadioPort $Port -ProcessId $radioProcess.Id }
            catch { Write-Warning ('Could not record the radio session: ' + $_.Exception.Message) }
        }
        Write-Host 'AI Radio started.'
        Write-Host 'To stop: run stop.cmd with the same -Port and -DataDir options.'
        if (-not $NoBrowser) { Open-RadioSession }
        exit 0
    }
    $radioProcess.Refresh()
    if ($radioProcess.HasExited) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-Command Remove-RadioRuntimeLease -ErrorAction SilentlyContinue) {
    try { Remove-RadioRuntimeLease -RadioPort $Port } catch { }
}
throw ('AI Radio did not become ready. See ' + (Join-Path $radioData 'server-stderr.log'))
