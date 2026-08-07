param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = "",
    [string]$NodeExecutable = "",
    [string]$HermesExecutable = "",
    [string]$InstallRoot = "$env:LOCALAPPDATA\m59-llm-bot",
    [string]$MeridianResourceDirectory = "",
    [string]$GameHost = "",
    [int]$GamePort = 5959,
    [string]$Timezone = "",
    [string]$ModelBaseUrl = "",
    [string]$ModelName = "",
    [System.Security.SecureString]$ModelApiKey,
    [bool]$ModelJsonMode = $true,
    [switch]$ModelDisableThinking,
    [string]$ObsidianVaultPath = "",
    [string]$DashboardBind = "127.0.0.1",
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$SkipHermes,
    [switch]$SkipScheduledTask
)

$ErrorActionPreference = "Stop"
$installRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$dataRoot = Join-Path $installRoot "data"
$logRoot = Join-Path $installRoot "logs"
$runRoot = Join-Path $installRoot "run"
$configPath = Join-Path $installRoot "bot.toml"
$secretPath = Join-Path $installRoot "secrets.env"
$rscRoot = Join-Path $dataRoot "rsc"

function Resolve-InstallExecutable {
    param(
        [string]$Explicit,
        [string]$CommandName,
        [string]$BundledFallback = "",
        [switch]$Optional
    )
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit -PathType Leaf)) {
            throw "Executable not found: $Explicit"
        }
        return [System.IO.Path]::GetFullPath($Explicit)
    }
    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if ($BundledFallback -and (Test-Path -LiteralPath $BundledFallback -PathType Leaf)) {
        return [System.IO.Path]::GetFullPath($BundledFallback)
    }
    if ($Optional) { return "" }
    throw "Required executable '$CommandName' was not found on PATH. Pass its path explicitly."
}

$PythonExecutable = Resolve-InstallExecutable $PythonExecutable "python" "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"
$NodeExecutable = Resolve-InstallExecutable $NodeExecutable "node" "$env:LOCALAPPDATA\hermes\node\node.exe"
$HermesExecutable = Resolve-InstallExecutable $HermesExecutable "hermes" "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe" -Optional

function Read-InstallSetting {
    param([string]$Value, [string]$Prompt, [string]$Default = "")
    if ($Value) { return $Value }
    $label = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    $entered = Read-Host $label
    if ($entered) { return $entered }
    return $Default
}

function Assert-SingleLineSetting {
    param([string]$Name, [string]$Value)
    if (-not $Value -or $Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "$Name must be a non-empty single-line value without double quotes"
    }
}

$GameHost = Read-InstallSetting $GameHost "Meridian 59 server host" "127.0.0.1"
$Timezone = Read-InstallSetting $Timezone "IANA timezone for status and journals" "UTC"
$ModelBaseUrl = Read-InstallSetting $ModelBaseUrl "OpenAI-compatible LLM base URL" "http://127.0.0.1:8000/v1"
$ModelName = Read-InstallSetting $ModelName "LLM model ID"
foreach ($setting in @(
    @{ Name = "GameHost"; Value = $GameHost },
    @{ Name = "Timezone"; Value = $Timezone },
    @{ Name = "ModelBaseUrl"; Value = $ModelBaseUrl },
    @{ Name = "ModelName"; Value = $ModelName },
    @{ Name = "DashboardBind"; Value = $DashboardBind }
)) {
    Assert-SingleLineSetting $setting.Name $setting.Value
}

function Find-MeridianResourceDirectory {
    if ($MeridianResourceDirectory) {
        if (-not (Test-Path -LiteralPath $MeridianResourceDirectory -PathType Container)) {
            throw "Meridian resource directory not found: $MeridianResourceDirectory"
        }
        return [System.IO.Path]::GetFullPath($MeridianResourceDirectory)
    }

    $steamRoots = [System.Collections.Generic.List[string]]::new()
    foreach ($key in @(
        "HKCU:\Software\Valve\Steam",
        "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam",
        "HKLM:\SOFTWARE\Valve\Steam"
    )) {
        try {
            $properties = Get-ItemProperty -Path $key -ErrorAction Stop
            foreach ($name in @("SteamPath", "InstallPath")) {
                if ($properties.$name) { $steamRoots.Add([string]$properties.$name) }
            }
        } catch {}
    }

    $libraries = [System.Collections.Generic.List[string]]::new()
    foreach ($steamRoot in ($steamRoots | Sort-Object -Unique)) {
        $libraries.Add($steamRoot)
        $libraryFile = Join-Path $steamRoot "steamapps\libraryfolders.vdf"
        if (Test-Path -LiteralPath $libraryFile) {
            foreach ($line in Get-Content -LiteralPath $libraryFile) {
                if ($line -match '"path"\s+"([^"]+)"') {
                    $libraries.Add($matches[1].Replace('\\', '\'))
                }
            }
        }
    }

    foreach ($library in ($libraries | Sort-Object -Unique)) {
        $manifest = Join-Path $library "steamapps\appmanifest_893390.acf"
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { continue }
        $installDirectory = "Meridian 59"
        $installLine = Get-Content -LiteralPath $manifest | Where-Object { $_ -match '"installdir"\s+"([^"]+)"' } | Select-Object -First 1
        if ($installLine -and $installLine -match '"installdir"\s+"([^"]+)"') { $installDirectory = $matches[1] }
        $candidate = Join-Path $library "steamapps\common\$installDirectory\resource"
        if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
    }
    return $null
}

foreach ($directory in @($installRoot, $dataRoot, $logRoot, $runRoot, $rscRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$clientResourceRoot = Find-MeridianResourceDirectory
if ($clientResourceRoot) {
    $looseResourceFiles = @(Get-ChildItem -LiteralPath $clientResourceRoot -File -Filter "*.rsc")
    foreach ($resourceFile in $looseResourceFiles) {
        Copy-Item -LiteralPath $resourceFile.FullName -Destination (Join-Path $rscRoot $resourceFile.Name) -Force
    }
    $retailBundle = Join-Path $clientResourceRoot "rsc0000.rsb"
    if (Test-Path -LiteralPath $retailBundle -PathType Leaf) {
        # The Steam client uses the normal RSC binary format but packages the
        # string table with an .rsb suffix. The harness intentionally scans
        # .rsc files, so stage a private extension-correct copy.
        Copy-Item -LiteralPath $retailBundle -Destination (Join-Path $rscRoot "rsc0000.rsc") -Force
    }
    Write-Host "Meridian resource table staged from: $clientResourceRoot"
} else {
    Write-Warning "Meridian 59 Steam resources were not found; name-based gameplay will be degraded."
}

$credential = $Credential
if (-not $credential) {
    $username = Read-Host "Meridian 59 account username"
    $securePassword = Read-Host "Meridian 59 account password" -AsSecureString
    $credential = New-Object System.Management.Automation.PSCredential($username, $securePassword)
}
$username = $credential.UserName
$plainPassword = $credential.GetNetworkCredential().Password
$controlTokenBytes = New-Object byte[] 32
$random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $random.GetBytes($controlTokenBytes) } finally { $random.Dispose() }
$controlToken = [Convert]::ToBase64String($controlTokenBytes).TrimEnd('=').Replace('+','-').Replace('/','_')

$modelApiKeyPlain = ""
if (-not $ModelApiKey) {
    $ModelApiKey = Read-Host "LLM API key (leave blank when the endpoint needs none)" -AsSecureString
}
if ($ModelApiKey) {
    $modelApiKeyCredential = New-Object System.Management.Automation.PSCredential("model", $ModelApiKey)
    $modelApiKeyPlain = $modelApiKeyCredential.GetNetworkCredential().Password
}
if ($ObsidianVaultPath -and ($ObsidianVaultPath.Contains("`r") -or $ObsidianVaultPath.Contains("`n"))) {
    throw "ObsidianVaultPath must be a single-line path"
}

$secretLines = @(
    "M59_ACCOUNT_USERNAME=$username"
    "M59_ACCOUNT_PASSWORD=$plainPassword"
    "M59_BOT_CONTROL_TOKEN=$controlToken"
    "M59_LLM_API_KEY=$modelApiKeyPlain"
    "M59_OBSIDIAN_VAULT_PATH=$ObsidianVaultPath"
)
[System.IO.File]::WriteAllLines($secretPath, $secretLines, [System.Text.UTF8Encoding]::new($false))
$plainPassword = $null
$modelApiKeyPlain = $null
$secretLines = $null
$acl = Get-Acl -LiteralPath $secretPath
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($env:USERNAME, "FullControl", "Allow")
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $secretPath -AclObject $acl

$harnessRoot = Join-Path $ProjectRoot "vendor\m59-harness"
$escapedProject = $ProjectRoot.Replace('\', '/')
$escapedHarness = $harnessRoot.Replace('\', '/')
$escapedNode = $NodeExecutable.Replace('\', '/')
$escapedInstall = $installRoot.Replace('\', '/')
$obsidianEnabled = if ($ObsidianVaultPath) { "true" } else { "false" }
$config = @"
[deployment]
instance_id = "primary"
timezone = "$Timezone"
data_dir = "$escapedInstall/data"
log_dir = "$escapedInstall/logs"
run_dir = "$escapedInstall/run"
secret_file = "$escapedInstall/secrets.env"

[game]
host = "$GameHost"
port = $GamePort
agent = "primary"
account_alias = "primary"
character = ""
autojoin = true

[harness]
root = "$escapedHarness"
expected_revision = "afeb5f3e67673643547c2c9aa245e01a69035af0"
control_url = "http://127.0.0.1:8901"
dashboard_port = 8902
lifecycle = "controller_managed"
node_executable = "$escapedNode"
state_file = "$escapedInstall/data/harness-fleet-state.json"

[model]
base_url = "$ModelBaseUrl"
name = "$ModelName"
planner_timeout_seconds = 90
responder_timeout_seconds = 45
max_output_tokens = 1200
temperature = 0.2
json_mode = $($ModelJsonMode.ToString().ToLowerInvariant())
disable_thinking = $($ModelDisableThinking.IsPresent.ToString().ToLowerInvariant())

[controller]
control_bind = "127.0.0.1"
control_port = 8903
dashboard_bind = "$DashboardBind"
dashboard_port = 8904
active_cadence_seconds = 3
idle_cadence_seconds = 30
error_backoff_max_seconds = 60
fallback_mode = "survive"
conversation_enabled = true
social_poll_seconds = 1
proactive_greetings_enabled = true
greeting_cooldown_seconds = 1200
greetings_per_minute = 20
conversation_history_turns = 8
minimum_goal_commitment_seconds = 3600
minimum_stall_seconds = 300

[onboarding]
enabled = true
create_from_persona = true
preserve_existing_character = true

[policy]
avoid_death = true
bank_before_hazard = true
rest_health_fraction = 0.70
critical_health_fraction = 0.40
carried_currency_bank_threshold = 1
protected_item_value_threshold = 5000
protected_item_names = []
consequential_action_guidance = "strongly_avoid_unnecessary_loss"

[learning]
enabled = true
no_progress_budget = 4
repeated_tactic_budget = 3
wait_budget = 6
survival_interrupt_budget = 3
world_retry_cooldown_seconds = 1800
generic_retry_cooldown_seconds = 3600

[notifications]
windows_enabled = true
minimum_severity = "notice"
obsidian_enabled = $obsidianEnabled
obsidian_vault_path = ""
obsidian_project_relative_path = "01 Projects/Meridian 59 Bot"
obsidian_index_filename = "Meridian 59 Bot.md"
obsidian_journal_subdirectory = "Journal"
obsidian_assessment_batch_size = 20
"@
[System.IO.File]::WriteAllText($configPath, $config, [System.Text.UTF8Encoding]::new($false))

if (-not $SkipScheduledTask) {
    $launcher = Join-Path $ProjectRoot "scripts\run-controller.ps1"
    $arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -ProjectRoot `"$ProjectRoot`" -PythonExecutable `"$PythonExecutable`" -ConfigPath `"$configPath`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName "Meridian59 LLM Bot" -Action $action -Trigger $trigger -Settings $settings -Description "24/7 local Meridian 59 LLM controller" -Force | Out-Null
}

$registerHermes = (-not $SkipHermes) -and [bool]$HermesExecutable
if ((-not $SkipHermes) -and (-not $HermesExecutable)) {
    Write-Warning "Hermes CLI was not found; skipping MCP registration. Run the installer again after installing Hermes or register the MCP commands in another compatible host."
}
if ($registerHermes) {
    $existing = & $HermesExecutable mcp list 2>&1 | Out-String
    if ($existing -match '\bmeridian_bot\b') {
        "Y" | & $HermesExecutable mcp remove meridian_bot | Out-Null
    }
    if ($existing -match '\bmeridian_knowledge\b') {
        "Y" | & $HermesExecutable mcp remove meridian_knowledge | Out-Null
    }
    "Y" | & $HermesExecutable mcp add meridian_bot --command $PythonExecutable --env "PYTHONPATH=$(Join-Path $ProjectRoot 'src')" "M59_BOT_SECRET_FILE=$secretPath" "M59_BOT_CONTROL_URL=http://127.0.0.1:8903" --args -m meridian_bot.cli mcp
    if ($LASTEXITCODE -ne 0) { throw "Hermes MCP registration failed" }
    "Y" | & $HermesExecutable mcp add meridian_knowledge --command $PythonExecutable --env "PYTHONPATH=$(Join-Path $ProjectRoot 'src')" "M59_BOT_SECRET_FILE=$secretPath" "M59_BOT_CONTROL_URL=http://127.0.0.1:8903" --args -m meridian_bot.cli knowledge-mcp
    if ($LASTEXITCODE -ne 0) { throw "Hermes knowledge MCP registration failed" }
}

if (-not $SkipScheduledTask) { Start-ScheduledTask -TaskName "Meridian59 LLM Bot" }
Write-Host "Installed. Controller data: $installRoot"
if ($registerHermes) { Write-Host "Restart Hermes to load mcp_meridian_bot_* and mcp_meridian_knowledge_* tools." }
if (-not $SkipScheduledTask) { Write-Host "Dashboard: http://$DashboardBind`:8904/" }
Write-Host "Next: restart the higher-level agent, set the character persona/name, then wait for onboarding to report ready_for_goals=true."
