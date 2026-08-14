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
    [ValidateSet("none", "bearer", "anthropic")]
    [string]$ModelAuthMode = "",
    [System.Security.SecureString]$ModelApiKey,
    [bool]$ModelJsonMode = $true,
    [switch]$ModelDisableThinking,
    [switch]$ModelEnableThinking,
    [string]$ObsidianVaultPath = "",
    [string]$DashboardBind = "127.0.0.1",
    [string]$PersonaFile = "",
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$SkipPersonaSetup,
    [switch]$SkipTui,
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

function Protect-UserOnlyFile {
    param([string]$Path)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $identity.User) {
        throw "The current Windows identity has no security identifier"
    }
    $security = New-Object System.Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity.User,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $security.AddAccessRule($rule) | Out-Null
    Set-Acl -LiteralPath $Path -AclObject $security

    $verified = Get-Acl -LiteralPath $Path
    $rules = @($verified.GetAccessRules(
        $true,
        $false,
        [System.Security.Principal.SecurityIdentifier]
    ))
    if (-not $verified.AreAccessRulesProtected -or $rules.Count -ne 1 -or
        $rules[0].IdentityReference -ne $identity.User -or
        $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        ($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl) {
        throw "Could not verify user-only permissions on $Path"
    }
}

function Resolve-IanaTimezoneAlias {
    param([string]$Value)
    $trimmed = $Value.Trim()
    $aliases = @{
        "UTC" = "UTC"
        "GMT" = "UTC"
        "PST" = "America/Los_Angeles"
        "PDT" = "America/Los_Angeles"
        "PT" = "America/Los_Angeles"
        "Pacific Standard Time" = "America/Los_Angeles"
        "MST" = "America/Denver"
        "MDT" = "America/Denver"
        "MT" = "America/Denver"
        "Mountain Standard Time" = "America/Denver"
        "US Mountain Standard Time" = "America/Phoenix"
        "CST" = "America/Chicago"
        "CDT" = "America/Chicago"
        "CT" = "America/Chicago"
        "Central Standard Time" = "America/Chicago"
        "EST" = "America/New_York"
        "EDT" = "America/New_York"
        "ET" = "America/New_York"
        "Eastern Standard Time" = "America/New_York"
        "Alaskan Standard Time" = "America/Anchorage"
        "Hawaiian Standard Time" = "Pacific/Honolulu"
    }
    if ($aliases.ContainsKey($trimmed)) { return $aliases[$trimmed] }
    return $trimmed
}

function Test-IanaTimezone {
    param([string]$Value)
    if (-not $Value) { return $false }
    $previousErrorPreference = $ErrorActionPreference
    $hasNativePreference = Test-Path Variable:PSNativeCommandUseErrorActionPreference
    if ($hasNativePreference) {
        $previousNativePreference = $PSNativeCommandUseErrorActionPreference
    }
    try {
        $ErrorActionPreference = "SilentlyContinue"
        if ($hasNativePreference) { $PSNativeCommandUseErrorActionPreference = $false }
        & $PythonExecutable -c "import sys; from zoneinfo import ZoneInfo; ZoneInfo(sys.argv[1])" $Value 2>$null
        $valid = $LASTEXITCODE -eq 0
        $global:LASTEXITCODE = 0
    } finally {
        $ErrorActionPreference = $previousErrorPreference
        if ($hasNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
    return $valid
}

function Read-InstallTimezone {
    param([string]$Value)
    if ($Value) {
        $resolved = Resolve-IanaTimezoneAlias $Value
        if (-not (Test-IanaTimezone $resolved)) {
            throw "Timezone '$Value' is not available. Use a valid IANA timezone such as America/Los_Angeles."
        }
        return $resolved
    }

    $choices = @(
        @{ Label = "UTC"; Value = "UTC" },
        @{ Label = "Pacific Time (PST/PDT)"; Value = "America/Los_Angeles" },
        @{ Label = "Mountain Time (MST/MDT)"; Value = "America/Denver" },
        @{ Label = "Arizona Time"; Value = "America/Phoenix" },
        @{ Label = "Central Time (CST/CDT)"; Value = "America/Chicago" },
        @{ Label = "Eastern Time (EST/EDT)"; Value = "America/New_York" },
        @{ Label = "Alaska Time"; Value = "America/Anchorage" },
        @{ Label = "Hawaii Time"; Value = "Pacific/Honolulu" }
    )
    $localIana = Resolve-IanaTimezoneAlias ([System.TimeZoneInfo]::Local.Id)
    $defaultIndex = 0
    for ($index = 0; $index -lt $choices.Count; $index++) {
        if ($choices[$index].Value -eq $localIana) { $defaultIndex = $index; break }
    }

    Write-Host "Timezone:"
    for ($index = 0; $index -lt $choices.Count; $index++) {
        $detected = if ($index -eq $defaultIndex) { " (detected/default)" } else { "" }
        Write-Host "  [$($index + 1)] $($choices[$index].Label) - $($choices[$index].Value)$detected"
    }
    Write-Host "  [M] Another IANA timezone"
    while ($true) {
        $entered = (Read-Host "Select timezone [$($defaultIndex + 1)]").Trim()
        if (-not $entered) { return $choices[$defaultIndex].Value }
        if ($entered.ToLowerInvariant() -eq "m") {
            $manual = Resolve-IanaTimezoneAlias (Read-Host "IANA timezone (for example Europe/London)")
            if (Test-IanaTimezone $manual) { return $manual }
            Write-Warning "That timezone is not available. Enter an IANA name such as Europe/London."
            continue
        }
        $selection = 0
        if ([int]::TryParse($entered, [ref]$selection) -and
            $selection -ge 1 -and $selection -le $choices.Count) {
            return $choices[$selection - 1].Value
        }
        Write-Warning "Choose a number from 1 through $($choices.Count), or M for another IANA timezone."
    }
}

function ConvertFrom-InstallSecureString {
    param([System.Security.SecureString]$Value)
    if (-not $Value) { return "" }
    $keyCredential = New-Object System.Management.Automation.PSCredential("model", $Value)
    return $keyCredential.GetNetworkCredential().Password
}

function Get-OpenAiModelIds {
    param(
        [string]$BaseUrl,
        [string]$ApiKey = "",
        [ValidateSet("none", "bearer", "anthropic")]
        [string]$AuthMode = "none"
    )
    $modelsUrl = "$($BaseUrl.TrimEnd('/'))/models"
    $headers = @{}
    if ($AuthMode -eq "bearer" -and $ApiKey) {
        $headers.Authorization = "Bearer $ApiKey"
    } elseif ($AuthMode -eq "anthropic" -and $ApiKey) {
        $headers["x-api-key"] = $ApiKey
        $headers["anthropic-version"] = "2023-06-01"
    }
    try {
        $response = Invoke-RestMethod -Method Get -Uri $modelsUrl -Headers $headers -TimeoutSec 10
    } catch {
        Write-Warning "Could not query $modelsUrl`: $($_.Exception.Message)"
        return @()
    }
    $items = if ($null -ne $response.data) { @($response.data) } else { @($response) }
    return @(
        $items |
            ForEach-Object { if ($null -ne $_.id) { [string]$_.id } } |
            Where-Object { $_ } |
            Sort-Object -Unique
    )
}

function Read-ModelAuthMode {
    param(
        [string]$Value,
        [string]$BaseUrl,
        [bool]$HasApiKey
    )
    if ($Value) { return $Value }
    $default = if ($BaseUrl -match '(?i)api\.anthropic\.com') {
        "anthropic"
    } elseif ($BaseUrl -match '(?i)api\.openai\.com' -or $HasApiKey) {
        "bearer"
    } else {
        "none"
    }
    Write-Host "LLM authentication:"
    Write-Host "  [1] None (local vLLM or an unauthenticated compatible endpoint)"
    Write-Host "  [2] Bearer API key (OpenAI/Codex or another compatible provider)"
    Write-Host "  [3] Anthropic API key (Claude; x-api-key plus API version)"
    $defaultNumber = @{ none = "1"; bearer = "2"; anthropic = "3" }[$default]
    while ($true) {
        $entered = (Read-Host "Select authentication [$defaultNumber]").Trim()
        if (-not $entered) { return $default }
        if ($entered -eq "1") { return "none" }
        if ($entered -eq "2") { return "bearer" }
        if ($entered -eq "3") { return "anthropic" }
        Write-Warning "Choose 1, 2, or 3."
    }
}

function Select-OpenAiModel {
    param([string[]]$ModelIds)
    Write-Host "Models reported by the endpoint:"
    for ($index = 0; $index -lt $ModelIds.Count; $index++) {
        Write-Host "  [$($index + 1)] $($ModelIds[$index])"
    }
    Write-Host "  [M] Enter a model ID manually"
    while ($true) {
        $entered = (Read-Host "Select model [1]").Trim()
        if (-not $entered) { return $ModelIds[0] }
        if ($entered.ToLowerInvariant() -eq "m") {
            return Read-InstallSetting "" "LLM model ID"
        }
        $selection = 0
        if ([int]::TryParse($entered, [ref]$selection) -and
            $selection -ge 1 -and $selection -le $ModelIds.Count) {
            return $ModelIds[$selection - 1]
        }
        Write-Warning "Choose a number from 1 through $($ModelIds.Count), or M for manual entry."
    }
}

function Read-ModelThinkingPreference {
    param([string]$ModelName)
    $qwenRecommended = $ModelName -match '(?i)(^|[/_-])qwen'
    $default = if ($qwenRecommended) { "1" } else { "2" }
    Write-Host "Model reasoning mode:"
    Write-Host "  [1] Disable thinking for fast structured controller JSON$(if ($qwenRecommended) { ' (recommended for Qwen)' } else { '' })"
    Write-Host "  [2] Keep the model's thinking mode enabled"
    Write-Host "Thinking tokens count against the completion limit and can delay or prevent the final JSON response."
    while ($true) {
        $entered = (Read-Host "Select reasoning mode [$default]").Trim()
        if (-not $entered) { return $default -eq "1" }
        if ($entered -eq "1") { return $true }
        if ($entered -eq "2") { return $false }
        Write-Warning "Choose 1 or 2."
    }
}

$GameHost = Read-InstallSetting $GameHost "Meridian 59 server host" "127.0.0.1"
$Timezone = Read-InstallTimezone $Timezone
$ModelBaseUrl = Read-InstallSetting $ModelBaseUrl "OpenAI-compatible LLM base URL" "http://127.0.0.1:8000/v1"
$modelApiKeyPlain = ConvertFrom-InstallSecureString $ModelApiKey
$ModelAuthMode = Read-ModelAuthMode $ModelAuthMode $ModelBaseUrl ([bool]$modelApiKeyPlain)
if ($ModelAuthMode -ne "none" -and -not $modelApiKeyPlain) {
    $providerLabel = if ($ModelAuthMode -eq "anthropic") { "Anthropic/Claude" } else { "Bearer/OpenAI" }
    $ModelApiKey = Read-Host "$providerLabel API key" -AsSecureString
    $modelApiKeyPlain = ConvertFrom-InstallSecureString $ModelApiKey
    if (-not $modelApiKeyPlain) {
        throw "$providerLabel authentication requires a non-empty API key"
    }
}
if (-not $ModelName) {
    $modelIds = @(Get-OpenAiModelIds $ModelBaseUrl $modelApiKeyPlain $ModelAuthMode)
    if ($modelIds.Count -gt 0) {
        $ModelName = Select-OpenAiModel $modelIds
    } else {
        Write-Warning "No model list was available; enter the exact model ID manually."
        $ModelName = Read-InstallSetting $ModelName "LLM model ID"
    }
}
if ($ModelDisableThinking -and $ModelEnableThinking) {
    throw "ModelDisableThinking and ModelEnableThinking cannot both be supplied"
}
$modelThinkingDisabled = if ($ModelDisableThinking) {
    $true
} elseif ($ModelEnableThinking) {
    $false
} else {
    Read-ModelThinkingPreference $ModelName
}
foreach ($setting in @(
    @{ Name = "GameHost"; Value = $GameHost },
    @{ Name = "Timezone"; Value = $Timezone },
    @{ Name = "ModelBaseUrl"; Value = $ModelBaseUrl },
    @{ Name = "ModelName"; Value = $ModelName },
    @{ Name = "ModelAuthMode"; Value = $ModelAuthMode },
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

$modelApiKeyPlain = ConvertFrom-InstallSecureString $ModelApiKey
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
Protect-UserOnlyFile $secretPath

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
expected_revision = "8363f8582c154f00be5db13e7d6d7353e50868c8"
control_url = "http://127.0.0.1:8901"
dashboard_port = 8902
lifecycle = "controller_managed"
node_executable = "$escapedNode"
state_file = "$escapedInstall/data/harness-fleet-state.json"

[model]
base_url = "$ModelBaseUrl"
name = "$ModelName"
auth_mode = "$ModelAuthMode"
planner_timeout_seconds = 120
responder_timeout_seconds = 45
max_output_tokens = 4096
temperature = 0.2
chat_temperature = 0.7
json_mode = $($ModelJsonMode.ToString().ToLowerInvariant())
disable_thinking = $($modelThinkingDisabled.ToString().ToLowerInvariant())

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
conversation_window_messages = 12
conversation_window_seconds = 1800
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

if (-not $SkipPersonaSetup) {
    $personaArguments = @(
        "-m",
        "meridian_bot.cli",
        "--config",
        $configPath,
        "setup-persona"
    )
    if ($PersonaFile) {
        if (-not (Test-Path -LiteralPath $PersonaFile -PathType Leaf)) {
            throw "Persona JSON file not found: $PersonaFile"
        }
        $personaArguments += @("--input", [System.IO.Path]::GetFullPath($PersonaFile))
    }
    & $PythonExecutable @personaArguments
    if ($LASTEXITCODE -ne 0) { throw "Persona setup failed" }
}

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
if ($SkipPersonaSetup) {
    Write-Host "Next: run 'm59-bot --config `"$configPath`" setup-persona', then wait for onboarding to report ready_for_goals=true."
} else {
    Write-Host "Character onboarding is configured. Wait for onboarding to report ready_for_goals=true, then submit the first strategic goal."
    Write-Host "If an established character requires explicit replacement, run setup-persona with --update-existing --reuse-current --replace-existing-character."
}
if ((-not $SkipScheduledTask) -and (-not $SkipTui)) {
    Write-Host "Opening the goal monitoring console. Press Q to leave the console; the controller will continue running."
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    & $PythonExecutable -m meridian_bot.cli --config $configPath tui
    if ($LASTEXITCODE -ne 0) { throw "Goal monitoring console failed" }
}
