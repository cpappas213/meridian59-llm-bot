param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = "",
    [string]$InstallRoot = "$env:LOCALAPPDATA\m59-llm-bot",
    [string]$ConfigPath = "",
    [ValidateRange(5, 600)]
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

if (-not $PythonExecutable) {
    $projectPython = Join-Path $resolvedProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        $PythonExecutable = $projectPython
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) {
            $PythonExecutable = $command.Source
        } else {
            $hermesPython = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe"
            if (Test-Path -LiteralPath $hermesPython -PathType Leaf) {
                $PythonExecutable = $hermesPython
            } else {
                throw "Python was not found. Create .venv or pass -PythonExecutable."
            }
        }
    }
}
$PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)

$hasConfig = Test-Path -LiteralPath $resolvedConfigPath -PathType Leaf
if (-not $hasConfig) {
    Write-Host "No installed bot was found. Starting one-time setup."
    & (Join-Path $resolvedProjectRoot "scripts\install.ps1") `
        -ProjectRoot $resolvedProjectRoot `
        -PythonExecutable $PythonExecutable `
        -InstallRoot $resolvedInstallRoot `
        -SkipTui
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "Meridian 59 bot installation found."
    Write-Host "  [1] Open the live goal monitoring console"
    Write-Host "  [2] Re-run configuration (existing persona and runtime are preserved)"
    Write-Host "  [Q] Quit"
    $selection = (Read-Host "Select [1]").Trim().ToLowerInvariant()
    if ($selection -eq "q") { exit 0 }
    if ($selection -eq "2") {
        & (Join-Path $resolvedProjectRoot "scripts\install.ps1") `
            -ProjectRoot $resolvedProjectRoot `
            -PythonExecutable $PythonExecutable `
            -InstallRoot $resolvedInstallRoot `
            -SkipTui
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } elseif ($selection -notin @("", "1")) {
        throw "Unknown selection: $selection"
    }
}

$task = Get-ScheduledTask -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
$env:PYTHONPATH = Join-Path $resolvedProjectRoot "src"

function Invoke-ControllerStatus {
    param([switch]$RequireJoined)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Unavailable is expected while the scheduled or standalone controller
        # is starting, so capture the native exit code instead of terminating.
        $ErrorActionPreference = "Continue"
        $arguments = @("-m", "meridian_bot.cli", "--config", $resolvedConfigPath, "status")
        if ($RequireJoined) { $arguments += "--require-joined" }
        $output = @(& $PythonExecutable @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

$probe = Invoke-ControllerStatus
$standaloneController = $null
if ($probe.ExitCode -ne 0) {
    if ($task) {
        if ($task.State -ne "Running") {
            Write-Host "Starting the Meridian 59 controller task..."
            Start-ScheduledTask -TaskName "Meridian59 LLM Bot"
        } else {
            Write-Host "The controller task is starting; waiting for its API..."
        }
    } else {
        # A development install may deliberately omit the scheduled task. TUI
        # is still an entry point, so start the same controller launcher as a
        # detached hidden process and leave it running when the console closes.
        Write-Host "No controller task is installed; starting the controller in the background..."
        $controllerLauncher = Join-Path $resolvedProjectRoot "scripts\run-controller.ps1"
        $controllerArguments = @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$controllerLauncher`"",
            "-ProjectRoot", "`"$resolvedProjectRoot`"",
            "-PythonExecutable", "`"$PythonExecutable`"",
            "-ConfigPath", "`"$resolvedConfigPath`""
        )
        $standaloneController = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $controllerArguments `
            -WorkingDirectory $resolvedProjectRoot `
            -WindowStyle Hidden `
            -PassThru
    }
}

$startupDeadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
$ready = Invoke-ControllerStatus -RequireJoined
while ($ready.ExitCode -ne 0 -and (Get-Date) -lt $startupDeadline) {
    if ($standaloneController -and $standaloneController.HasExited) {
        throw "The background controller exited before the broker joined the game. Check $resolvedInstallRoot\logs\controller.log."
    }
    if ($task) {
        $task = Get-ScheduledTask -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
        if ($task -and $task.State -ne "Running" -and (Invoke-ControllerStatus).ExitCode -ne 0) {
            $taskInfo = Get-ScheduledTaskInfo -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
            $result = if ($taskInfo) { " Last task result: $($taskInfo.LastTaskResult)." } else { "" }
            throw "The controller task stopped before the broker joined the game.$result Check $resolvedInstallRoot\logs\controller.log."
        }
    }
    Start-Sleep -Seconds 1
    $ready = Invoke-ControllerStatus -RequireJoined
}
if ($ready.ExitCode -ne 0) {
    throw "The controller did not reach a joined game session within $StartupTimeoutSeconds seconds. It was left running for diagnosis. Check $resolvedInstallRoot\logs\controller.log."
}

Write-Host "Controller, broker, and game session ready."
Set-Location -LiteralPath $resolvedProjectRoot
& $PythonExecutable -m meridian_bot.cli --config $resolvedConfigPath tui
exit $LASTEXITCODE
