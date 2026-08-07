param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = "",
    [string]$ConfigPath = "$env:LOCALAPPDATA\m59-llm-bot\bot.toml"
)

$ErrorActionPreference = "Stop"
$PythonExecutable = if ($PythonExecutable) {
    $PythonExecutable
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$sourceRoot = Join-Path $ProjectRoot "src"
$env:PYTHONPATH = $sourceRoot
# Make the workspace source directory sys.path[0] as well as PYTHONPATH so a
# separately installed older package can never shadow the scheduled build.
Set-Location -LiteralPath $sourceRoot
& $PythonExecutable -m meridian_bot.cli --config $ConfigPath run
exit $LASTEXITCODE
