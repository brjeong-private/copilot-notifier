#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
$local = Join-Path $PSScriptRoot '..\.local'
$config = Get-NotifierConfig -Path (Join-Path $local 'config.json')
$receipts = Join-Path $local 'receipts'
$null = New-Item -ItemType Directory -Path $receipts -Force
$operationId = [Guid]::NewGuid().ToString()
$content = Get-NotificationContent -Project 'tools' -Task 'copilot-notifier readiness check' `
    -Summary 'Azure email is ready. Only accepted Autopilot completions trigger notifications.'
Send-CompletionEmail -Config $config -Subject $content.subject -Summary $content.body `
    -OperationId $operationId -ReceiptPath (Join-Path $receipts "$operationId.json")
Write-Output 'ACS confirmed a successful test send operation. Inbox delivery is not independently confirmed.'
