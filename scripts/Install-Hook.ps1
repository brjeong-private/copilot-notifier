#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$homePath = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $HOME '.copilot' }
$hooksDir = Join-Path $homePath 'hooks'
$target = Join-Path $hooksDir 'tools-copilot-notifier.json'
$script = Join-Path $PSScriptRoot 'Notify-Completion.ps1'
$executable = (Get-Command pwsh -ErrorAction Stop).Source
$command = '& "{0}" -NoProfile -NonInteractive -File "{1}"' -f $executable, $script
$legacyScript = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'copilot-notifier\scripts\Notify-Completion.ps1'
$legacyCommand = '& "{0}" -NoProfile -NonInteractive -File "{1}"' -f $executable, $legacyScript
if (Test-Path -LiteralPath $target) {
    $existing = Get-Content -LiteralPath $target -Raw | ConvertFrom-Json -AsHashtable
    if ($existing.hooks.Stop[0].powershell -notin @($command, $legacyCommand)) {
        throw 'A different hook already uses this filename. It has not been overwritten.'
    }
}
$null = New-Item -ItemType Directory -Path $hooksDir -Force
@{
    version = 1
    hooks = @{
        Stop = @(@{ type = 'command'; powershell = $command; timeoutSec = 90 })
    }
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $target -Encoding utf8
Write-Output 'User-level Stop hook installed. Restart Copilot CLI and reload VS Code to load it.'
