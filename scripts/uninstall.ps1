param(
    [string]$HermesExecutable = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe",
    [switch]$KeepHermesEntry
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "Meridian59 LLM Bot" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "Meridian59 LLM Bot" -Confirm:$false
}
if (-not $KeepHermesEntry -and (Test-Path -LiteralPath $HermesExecutable)) {
    "Y" | & $HermesExecutable mcp remove meridian_bot
    "Y" | & $HermesExecutable mcp remove meridian_knowledge
}
Write-Host "Autostart and both Hermes MCP registrations removed. Runtime data and credentials were retained under LocalAppData."
