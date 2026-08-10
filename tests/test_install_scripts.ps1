param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$installerPath = Join-Path $projectRoot "scripts\install.ps1"
$launcherPath = Join-Path $projectRoot "scripts\launch.ps1"

function Read-ScriptAst {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    $source = Get-Content -LiteralPath $Path -Raw
    $ast = [System.Management.Automation.Language.Parser]::ParseInput(
        $source,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count) {
        throw "$Path has a PowerShell syntax error: $($errors[0].Message)"
    }
    return $ast
}

$installerAst = Read-ScriptAst $installerPath
$null = Read-ScriptAst $launcherPath
$wanted = @(
    "Resolve-IanaTimezoneAlias",
    "Test-IanaTimezone",
    "Read-InstallTimezone",
    "Protect-UserOnlyFile",
    "Get-OpenAiModelIds"
)
$installerAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $wanted -contains $node.Name
}, $true) | ForEach-Object { Invoke-Expression $_.Extent.Text }

$PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)
if ((Resolve-IanaTimezoneAlias "PST") -ne "America/Los_Angeles") {
    throw "PST was not normalized to America/Los_Angeles"
}
if (-not (Test-IanaTimezone "America/Los_Angeles")) {
    throw "America/Los_Angeles was rejected"
}
if (Test-IanaTimezone "Definitely/Invalid") {
    throw "An invalid timezone was accepted"
}
function Read-Host {
    param([string]$Prompt)
    return "2"
}
if ((Read-InstallTimezone "") -ne "America/Los_Angeles") {
    throw "The Pacific Time menu choice did not return America/Los_Angeles"
}

$aclTestName = "m59-acl-installer-test-$([guid]::NewGuid().ToString('N')).tmp"
$aclTestPath = Join-Path ([System.IO.Path]::GetTempPath()) $aclTestName
[System.IO.Directory]::CreateDirectory((Split-Path -Parent $aclTestPath)) | Out-Null
[System.IO.File]::WriteAllText($aclTestPath, "test")
try {
    Protect-UserOnlyFile $aclTestPath
    $acl = Get-Acl -LiteralPath $aclTestPath
    if (-not $acl.AreAccessRulesProtected) {
        throw "The installer ACL test file still inherits access rules"
    }
} finally {
    if (Test-Path -LiteralPath $aclTestPath) {
        Remove-Item -LiteralPath $aclTestPath -Force
    }
}

function Invoke-RestMethod {
    param($Method, $Uri, $Headers, $TimeoutSec)
    $script:lastHeaders = $Headers
    return @{ data = @(@{ id = "mock-model" }) }
}

$null = Get-OpenAiModelIds "https://api.openai.com/v1" "test-key" "bearer"
if ($lastHeaders.Authorization -ne "Bearer test-key" -or
    $lastHeaders.ContainsKey("x-api-key")) {
    throw "Bearer discovery headers are incorrect"
}
$null = Get-OpenAiModelIds "https://api.anthropic.com/v1" "test-key" "anthropic"
if ($lastHeaders["x-api-key"] -ne "test-key" -or
    $lastHeaders["anthropic-version"] -ne "2023-06-01" -or
    $lastHeaders.ContainsKey("Authorization")) {
    throw "Anthropic discovery headers are incorrect"
}

Write-Host "Installer and launcher tests passed."
exit 0
