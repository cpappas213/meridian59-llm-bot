param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = "",
    [string]$InstallRoot = "$env:LOCALAPPDATA\m59-llm-bot",
    [string]$ConfigPath = ""
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

$installedTask = Get-ScheduledTask -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
$hasConfig = Test-Path -LiteralPath $resolvedConfigPath -PathType Leaf
if ((-not $hasConfig) -or (-not $installedTask)) {
    if ($hasConfig) {
        Write-Host "The previous setup did not finish. Resuming one-time setup."
    } else {
        Write-Host "No installed bot was found. Starting one-time setup."
    }
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
if (-not $task) {
    throw "The controller scheduled task is missing. Re-run setup from option 2."
}
$env:PYTHONPATH = Join-Path $resolvedProjectRoot "src"
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $null = & $PythonExecutable -m meridian_bot.cli --config $resolvedConfigPath status 2>$null
    $controllerAvailable = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($task.State -ne "Running" -and -not $controllerAvailable) {
    Start-ScheduledTask -TaskName "Meridian59 LLM Bot"
}

Set-Location -LiteralPath $resolvedProjectRoot
& $PythonExecutable -m meridian_bot.cli --config $resolvedConfigPath tui
exit $LASTEXITCODE
