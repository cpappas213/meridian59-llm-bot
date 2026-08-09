param(
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"
$PythonExecutable = if ($PythonExecutable) {
    (Get-Command $PythonExecutable -CommandType Application -ErrorAction Stop).Source
} else {
    (Get-Command python -CommandType Application -ErrorAction Stop).Source
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot "src"
Set-Location -LiteralPath $projectRoot
& $PythonExecutable -m compileall -q src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PythonExecutable -m unittest discover -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $projectRoot "tests\test_install_scripts.ps1") `
    -PythonExecutable $PythonExecutable
exit $LASTEXITCODE
