param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = "",
    [string]$InstallRoot = "$env:LOCALAPPDATA\m59-llm-bot",
    [string]$ConfigPath = "",
    [string]$TaskName = "Meridian59 LLM Bot",
    [ValidateRange(10, 600)]
    [int]$ShutdownTimeoutSeconds = 60,
    [ValidateRange(10, 1800)]
    [int]$SafeBoundaryTimeoutSeconds = 180,
    [ValidateRange(10, 600)]
    [int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$resolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$resolvedConfigPath = if ($ConfigPath) {
    [System.IO.Path]::GetFullPath($ConfigPath)
} else {
    Join-Path $resolvedInstallRoot "bot.toml"
}

if (-not (Test-Path -LiteralPath $resolvedConfigPath -PathType Leaf)) {
    throw "Controller configuration not found: $resolvedConfigPath"
}
if (-not $PythonExecutable) {
    $projectPython = Join-Path $resolvedProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        $PythonExecutable = $projectPython
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if (-not $command) { throw "Python was not found. Create .venv or pass -PythonExecutable." }
        $PythonExecutable = $command.Source
    }
}
$PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { throw "Scheduled task not found: $TaskName" }

$env:PYTHONPATH = Join-Path $resolvedProjectRoot "src"

function Invoke-ControllerCli {
    param([string[]]$Arguments)
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell surfaces a native process's stderr as ErrorRecord
        # objects. Controller-unavailable is an expected probe result while a
        # restart is in flight, so capture the exit code instead of terminating.
        $ErrorActionPreference = "Continue"
        $output = @(& $PythonExecutable -m meridian_bot.cli --config $resolvedConfigPath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

$before = Invoke-ControllerCli @("status")
if ($before.ExitCode -eq 0) {
    $safeDeadline = (Get-Date).AddSeconds($SafeBoundaryTimeoutSeconds)
    do {
        try {
            $snapshot = ($before.Output -join [Environment]::NewLine) | ConvertFrom-Json
        } catch {
            throw "Controller status was not valid JSON: $($before.Output -join [Environment]::NewLine)"
        }
        $health = $snapshot.game.vitals.health
        $safeBoundary = (
            $snapshot.controller.foreground_action -eq $null -and
            $snapshot.game.risk -eq "low" -and
            $health.value -gt 0 -and
            $health.value -eq $health.max
        )
        if ($safeBoundary) { break }
        if ((Get-Date) -ge $safeDeadline) {
            throw "No full-health, low-risk maintenance boundary appeared within $SafeBoundaryTimeoutSeconds seconds; the running controller was left untouched."
        }
        Start-Sleep -Seconds 1
        $before = Invoke-ControllerCli @("status")
        if ($before.ExitCode -ne 0) {
            throw "The controller became unavailable while waiting for a safe maintenance boundary."
        }
    } while ($true)
    Write-Host "Requesting graceful controller and broker shutdown..."
    $stop = Invoke-ControllerCli @("stop")
    if ($stop.ExitCode -ne 0) {
        throw "Graceful controller stop failed: $($stop.Output -join [Environment]::NewLine)"
    }
} elseif ($task.State -eq "Running") {
    throw "The scheduled task is running but its authenticated controller API is unavailable. Refusing a blind task stop that could orphan child processes."
}

$shutdownDeadline = (Get-Date).AddSeconds($ShutdownTimeoutSeconds)
do {
    $task = Get-ScheduledTask -TaskName $TaskName
    $probe = Invoke-ControllerCli @("status")
    if ($task.State -ne "Running" -and $probe.ExitCode -ne 0) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $shutdownDeadline)
if ($task.State -eq "Running" -or $probe.ExitCode -eq 0) {
    throw "The controller did not stop cleanly within $ShutdownTimeoutSeconds seconds."
}

Write-Host "Starting scheduled controller..."
Start-ScheduledTask -TaskName $TaskName
$startupDeadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
$ready = $null
do {
    $task = Get-ScheduledTask -TaskName $TaskName
    $ready = Invoke-ControllerCli @("status", "--require-joined")
    if ($task.State -eq "Running" -and $ready.ExitCode -eq 0) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $startupDeadline)
if ($task.State -ne "Running" -or $ready.ExitCode -ne 0) {
    throw "The controller did not return to a joined state within $StartupTimeoutSeconds seconds."
}

Write-Host "Controller restarted cleanly and rejoined the game."
