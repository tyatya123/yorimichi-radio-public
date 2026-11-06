param(
    [Alias('Action')][ValidateSet('Start', 'Stop', 'Status')][string]$RuntimeAction = 'Status',
    [Alias('Port')][ValidateRange(1024, 65535)][int]$RuntimePort = 8765,
    [Alias('DataDir')][string]$RuntimeDataDir = ''
)

# Dot-source this file to use the functions without starting or stopping anything.
$script:RadioRuntimeRoot = Join-Path $PSScriptRoot 'runtime'
$script:RadioRuntimeState = Join-Path $script:RadioRuntimeRoot 'state'
$script:RadioRuntimeLogs = Join-Path $script:RadioRuntimeRoot 'logs'
$script:RadioRuntimeDefinitions = @{
    voicevox = @{
        Exe = Join-Path $script:RadioRuntimeRoot 'voicevox\windows-cpu\run.exe'
        Arguments = @('--host', '127.0.0.1', '--port', '50021', '--no-use_gpu', '--cpu_num_threads', '4', '--output_log_utf8', '--disable_mutable_api')
        Port = 50021
        Base = 'http://127.0.0.1:50021'
        Health = '/version'
        Setting = 'voicevox_url'
        WaitSeconds = 35
    }
    ollama = @{
        Exe = Join-Path $script:RadioRuntimeRoot 'ollama\ollama.exe'
        Arguments = @('serve')
        Port = 11434
        Base = 'http://127.0.0.1:11434'
        Health = '/api/version'
        Setting = 'ollama_url'
        WaitSeconds = 45
    }
}

function Initialize-RadioRuntimeState {
    New-Item -ItemType Directory -Path $script:RadioRuntimeState -Force | Out-Null
    New-Item -ItemType Directory -Path $script:RadioRuntimeLogs -Force | Out-Null
}

function Enter-RadioRuntimeLock {
    Initialize-RadioRuntimeState
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($script:RadioRuntimeRoot.ToLowerInvariant()))
    } finally { $hasher.Dispose() }
    $suffix = ([System.BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 24)
    $mutex = [System.Threading.Mutex]::new($false, ('Local\AIRadioRuntime-' + $suffix))
    try {
        try { $acquired = $mutex.WaitOne(15000) }
        catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'Another AI Radio launcher is managing the local engines. Try again shortly.' }
        return $mutex
    } catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-RadioRuntimeLock($Mutex) {
    try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function Read-RadioRuntimeOwners {
    $state = @{}
    $path = Join-Path $script:RadioRuntimeState 'helpers.json'
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            $saved = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($role in @('voicevox', 'ollama')) {
                if ($saved.$role) { $state[$role] = $saved.$role }
            }
        } catch {
            Write-Warning 'The engine ownership file cannot be read. Existing services will not be stopped.'
        }
    }
    return $state
}

function Write-RadioRuntimeJson([string]$Path, $Value) {
    $temporary = $Path + '.tmp'
    ConvertTo-Json -InputObject $Value -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Write-RadioRuntimeOwners($Owners) {
    Write-RadioRuntimeJson -Path (Join-Path $script:RadioRuntimeState 'helpers.json') -Value $Owners
}

function Get-RadioApplicationHealth([int]$Port) {
    try {
        $response = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/api/health') -TimeoutSec 1
        return ($response.app -eq 'ai-radio')
    } catch { return $false }
}

function Test-RadioRuntimeHealth([string]$Role) {
    $definition = $script:RadioRuntimeDefinitions[$Role]
    try {
        $response = Invoke-RestMethod -Uri ($definition.Base + $definition.Health) -TimeoutSec 1
        if ($Role -eq 'voicevox') { return ($response -is [string] -and $response -match '^\d+\.\d+') }
        return ($response.version -is [string] -and $response.version -match '^\d+\.\d+')
    } catch { return $false }
}

function Test-RadioRuntimePort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connecting = $client.ConnectAsync('127.0.0.1', $Port)
        return ($connecting.Wait(350) -and $client.Connected)
    } catch { return $false }
    finally { $client.Dispose() }
}

function Get-RadioOwnedProcess($Record, [string]$Role) {
    if (-not $Record) { return $null }
    try {
        $expected = [System.IO.Path]::GetFullPath($script:RadioRuntimeDefinitions[$Role].Exe)
        if (-not [string]::Equals([string]$Record.executable, $expected, [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
        $process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
        if (-not [string]::Equals($process.Path, $expected, [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
        if ([string]$process.StartTime.ToUniversalTime().Ticks -ne [string]$Record.started_ticks) { return $null }
        return $process
    } catch { return $null }
}

function Set-RadioRuntimeLease([int]$Port, [string]$State = 'pending', [int]$ProcessId = 0) {
    $lease = @{ port = $Port; state = $State; created_utc = [DateTime]::UtcNow.ToString('o') }
    if ($ProcessId -gt 0) {
        try {
            $process = Get-Process -Id $ProcessId -ErrorAction Stop
            $lease.pid = $process.Id
            $lease.started_ticks = [string]$process.StartTime.ToUniversalTime().Ticks
        } catch { }
    }
    Write-RadioRuntimeJson -Path (Join-Path $script:RadioRuntimeState ('radio-' + $Port + '.json')) -Value $lease
}

function Confirm-RadioRuntimeLease([int]$RadioPort, [int]$ProcessId = 0) {
    $mutex = Enter-RadioRuntimeLock
    try { Set-RadioRuntimeLease -Port $RadioPort -State 'running' -ProcessId $ProcessId }
    finally { Exit-RadioRuntimeLock $mutex }
}

function Remove-RadioRuntimeLease([int]$RadioPort) {
    $mutex = Enter-RadioRuntimeLock
    try { Remove-Item -LiteralPath (Join-Path $script:RadioRuntimeState ('radio-' + $RadioPort + '.json')) -ErrorAction SilentlyContinue }
    finally { Exit-RadioRuntimeLock $mutex }
}

function Get-RadioRuntimeConsumers([int]$ExceptPort = 0) {
    $consumers = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $script:RadioRuntimeState -Filter 'radio-*.json' -File -ErrorAction SilentlyContinue)) {
        try {
            $lease = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $port = [int]$lease.port
            if ($port -lt 1024 -or $port -gt 65535 -or $port -eq $ExceptPort) { continue }
            $alive = Get-RadioApplicationHealth $port
            if (-not $alive -and $lease.pid -and $lease.started_ticks) {
                try {
                    $process = Get-Process -Id ([int]$lease.pid) -ErrorAction Stop
                    $alive = ([string]$process.StartTime.ToUniversalTime().Ticks -eq [string]$lease.started_ticks)
                } catch { }
            }
            if (-not $alive -and $lease.state -eq 'pending') {
                $created = [DateTime]::Parse([string]$lease.created_utc).ToUniversalTime()
                $alive = (([DateTime]::UtcNow - $created).TotalSeconds -lt 120)
            }
            # A running radio without a known PID is retained conservatively.
            if (-not $alive -and $lease.state -eq 'running' -and -not $lease.pid) { $alive = $true }
            if ($alive) { $consumers += $port }
            else { Remove-Item -LiteralPath $file.FullName -ErrorAction SilentlyContinue }
        } catch {
            # Do not stop engines if a malformed ownership lease is ambiguous.
            $consumers += -1
        }
    }
    # Protect the normal port even if its radio was started manually, without a lease.
    if ($ExceptPort -ne 8765 -and $consumers -notcontains 8765 -and (Get-RadioApplicationHealth 8765)) { $consumers += 8765 }
    return @($consumers | Select-Object -Unique)
}

function Start-RadioManagedRuntimes([int]$RadioPort = 8765, [string]$DataDir = '', [switch]$RegisterLease) {
    $mutex = Enter-RadioRuntimeLock
    try {
        if ($RegisterLease) { Set-RadioRuntimeLease -Port $RadioPort }
        $settings = $null
        if ($DataDir) {
            $settingsPath = Join-Path $DataDir 'settings.json'
            if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
                try { $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
            }
        }
        $owners = Read-RadioRuntimeOwners
        foreach ($role in @('voicevox', 'ollama')) {
            $definition = $script:RadioRuntimeDefinitions[$role]
            $configured = if ($settings) { [string]$settings.($definition.Setting) } else { '' }
            if ($configured -and $configured.TrimEnd('/') -notin @($definition.Base, $definition.Base.Replace('127.0.0.1', 'localhost'))) {
                Write-Host ($role + ': a custom endpoint is configured; automatic startup is skipped.')
                continue
            }
            if (Test-RadioRuntimeHealth $role) {
                Write-Host ($role + ': using the running local service.')
                continue
            }
            if (Test-RadioRuntimePort $definition.Port) {
                Write-Warning ($role + ': its port is occupied. The existing process is left unchanged.')
                continue
            }
            if (-not (Test-Path -LiteralPath $definition.Exe -PathType Leaf)) { continue }
            if ($owners.ContainsKey($role) -and (Get-RadioOwnedProcess -Record $owners[$role] -Role $role)) {
                Write-Host ($role + ': the owned engine is still starting; it will not be duplicated.')
                continue
            }
            Write-Host ($role + ': starting the local engine...')
            $childEnvironment = @{}
            if ($role -eq 'ollama') {
                $modelDirectory = Join-Path $script:RadioRuntimeRoot 'models'
                New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
                $childEnvironment = @{
                    OLLAMA_HOST = '127.0.0.1:11434'; OLLAMA_MODELS = $modelDirectory
                    OLLAMA_NO_CLOUD = '1'; OLLAMA_NUM_PARALLEL = '1'; OLLAMA_MAX_LOADED_MODELS = '1'
                    OLLAMA_FLASH_ATTENTION = '1'; OLLAMA_KV_CACHE_TYPE = 'q8_0'; OLLAMA_KEEP_ALIVE = '3m'
                }
            }
            $previousEnvironment = @{}
            try {
                foreach ($name in $childEnvironment.Keys) {
                    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
                    [Environment]::SetEnvironmentVariable($name, [string]$childEnvironment[$name], 'Process')
                }
                $stdout = Join-Path $script:RadioRuntimeLogs ($role + '-stdout.log')
                $stderr = Join-Path $script:RadioRuntimeLogs ($role + '-stderr.log')
                foreach ($logFile in @($stdout, $stderr)) {
                    if ((Test-Path -LiteralPath $logFile -PathType Leaf) -and (Get-Item -LiteralPath $logFile).Length -gt 8MB) {
                        Move-Item -LiteralPath $logFile -Destination ($logFile + '.previous') -Force
                    }
                }
                $process = Start-Process -FilePath $definition.Exe -ArgumentList $definition.Arguments -WorkingDirectory (Split-Path -Parent $definition.Exe) -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
            } catch {
                Write-Warning ($role + ': could not start. The radio will use its fallback mode. ' + $_.Exception.Message)
                continue
            } finally {
                foreach ($name in $previousEnvironment.Keys) {
                    [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
                }
            }
            try {
                $owners[$role] = @{ pid = $process.Id; executable = [System.IO.Path]::GetFullPath($definition.Exe); started_ticks = [string]$process.StartTime.ToUniversalTime().Ticks }
                Write-RadioRuntimeOwners $owners
            } catch {
                Write-Warning ($role + ': ownership could not be recorded; automatic stop will skip it.')
            }
            $watch = [System.Diagnostics.Stopwatch]::StartNew()
            $ready = $false
            while ($watch.Elapsed.TotalSeconds -lt $definition.WaitSeconds) {
                if (Test-RadioRuntimeHealth $role) { $ready = $true; break }
                $process.Refresh()
                if ($process.HasExited) { break }
                Start-Sleep -Milliseconds 300
            }
            if ($ready) { Write-Host ($role + ': ready.') }
            else { Write-Warning ($role + ': not ready yet. See runtime/logs; the radio can still use fallback mode.') }
        }
    } finally { Exit-RadioRuntimeLock $mutex }
}

function Stop-RadioManagedRuntimes([int]$RadioPort = 8765, [switch]$RemoveLease) {
    $mutex = Enter-RadioRuntimeLock
    try {
        if ($RemoveLease) {
            Remove-Item -LiteralPath (Join-Path $script:RadioRuntimeState ('radio-' + $RadioPort + '.json')) -ErrorAction SilentlyContinue
        }
        $except = if ($RemoveLease) { $RadioPort } else { 0 }
        if (@(Get-RadioRuntimeConsumers -ExceptPort $except).Count -gt 0) {
            Write-Host 'Local engines are still used by another radio; they will keep running.'
            return
        }
        $owners = Read-RadioRuntimeOwners
        foreach ($role in @('ollama', 'voicevox')) {
            if (-not $owners.ContainsKey($role)) { continue }
            $process = Get-RadioOwnedProcess -Record $owners[$role] -Role $role
            if (-not $process) {
                $owners.Remove($role)
                continue
            }
            try {
                # Only this verified, app-owned process tree is stopped. Never kill by name.
                $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
                $result = Start-Process -FilePath $taskkill -ArgumentList @('/PID', [string]$process.Id, '/T', '/F') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $script:RadioRuntimeLogs ($role + '-stop.log')) -RedirectStandardError (Join-Path $script:RadioRuntimeLogs ($role + '-stop-errors.log'))
                if ($result.ExitCode -ne 0 -and (Get-RadioOwnedProcess -Record $owners[$role] -Role $role)) {
                    throw ($role + ': the owned process is still running.')
                }
                $owners.Remove($role)
                Write-Host ($role + ': stopped its app-owned process tree.')
            } catch {
                Write-Warning ($role + ': could not stop its owned process. ' + $_.Exception.Message)
            }
        }
        Write-RadioRuntimeOwners $owners
    } finally { Exit-RadioRuntimeLock $mutex }
}

function Get-RadioRuntimeStatus {
    $owners = Read-RadioRuntimeOwners
    foreach ($role in @('voicevox', 'ollama')) {
        $owned = if ($owners.ContainsKey($role)) { Get-RadioOwnedProcess -Record $owners[$role] -Role $role } else { $null }
        [PSCustomObject]@{ engine = $role; ready = (Test-RadioRuntimeHealth $role); owned_pid = $(if ($owned) { $owned.Id } else { $null }); executable = $script:RadioRuntimeDefinitions[$role].Exe }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    switch ($RuntimeAction) {
        'Start' { Start-RadioManagedRuntimes -RadioPort $RuntimePort -DataDir $RuntimeDataDir }
        'Stop' { Stop-RadioManagedRuntimes -RadioPort $RuntimePort }
        'Status' { Get-RadioRuntimeStatus | ConvertTo-Json -Depth 4 }
    }
}
