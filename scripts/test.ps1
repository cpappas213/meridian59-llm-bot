param(
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"
$PythonExecutable = if ($PythonExecutable) {
    $PythonExecutable
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot "src"
Set-Location -LiteralPath $projectRoot
& $PythonExecutable -m compileall -q src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExecutable -m unittest discover -v
exit $LASTEXITCODE
