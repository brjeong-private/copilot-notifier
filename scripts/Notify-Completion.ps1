#Requires -Version 7.0
param([string]$ConfigPath = (Join-Path $PSScriptRoot '..\.local\kakao.xml'))

$ErrorActionPreference = 'Stop'
$lock = $null
try {
    Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
    $hook = [Console]::In.ReadToEnd() | ConvertFrom-Json -AsHashtable
    if ($hook.agent_id -or $hook.agentId -or $hook.parent_agent_id -or $hook.parentAgentId) { exit 0 }
    if ($hook.hook_event_name -and $hook.hook_event_name -ne 'Stop') { exit 0 }
    $sessionId = if ($hook.session_id) { $hook.session_id } else { $hook.sessionId }
    $transcript = if ($hook.transcript_path) { $hook.transcript_path } else { $hook.transcriptPath }
    if (-not $sessionId -or -not $transcript) { throw 'Completion hook is missing session or transcript metadata.' }
    $candidate = Get-CompletionCandidate -TranscriptPath $transcript
    if (-not $candidate) { exit 0 }
    $project = Get-ProjectLabel -WorkingDirectory $hook.cwd
    if ([string]::IsNullOrWhiteSpace($candidate.task)) {
        throw 'The accepted completion has no human task title. No message was submitted.'
    }
    $content = Get-KakaoNotificationContent -Project $project -Task $candidate.task -Summary $candidate.summary `
        -StartedAt $candidate.startedAt -CompletedAt $candidate.timestamp
    $null = Get-KakaoConfig -Path $ConfigPath
    $stateDir = Join-Path (Split-Path $ConfigPath -Parent) 'kakao-receipts'
    $null = New-Item -ItemType Directory -Path $stateDir -Force
    $operationId = Get-OperationId -EventKey "$sessionId`:$($candidate.eventId)"
    $receiptPath = Join-Path $stateDir "$operationId.json"
    try {
        $lock = [IO.File]::Open((Join-Path $stateDir "$operationId.lock"),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        throw 'Another notifier is handling this completion. No duplicate message was submitted.'
    }
    if (Test-Path -LiteralPath $receiptPath) {
        $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
        if ($receipt.status -eq 'Succeeded') { exit 0 }
        throw 'A prior Kakao send failed or is uncertain. Inspect the receipt and chat before retrying.'
    }
    Send-CompletionKakao -ConfigPath $ConfigPath -Text $content `
        -OperationId $operationId -ReceiptPath $receiptPath
}
catch {
    [Console]::Error.WriteLine("Copilot notifier: $($_.Exception.Message)")
    exit 1
}
finally {
    if ($lock) { $lock.Dispose() }
}
