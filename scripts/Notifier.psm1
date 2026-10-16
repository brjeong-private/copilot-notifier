$ErrorActionPreference = 'Stop'

function Get-CompletionCandidate {
    param([Parameter(Mandatory)][string]$TranscriptPath)

    $mode = $null
    $candidate = $null
    $task = $null
    $startedAt = $null
    $sawSdkEvent = $false
    foreach ($line in [IO.File]::ReadLines($TranscriptPath)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $event = ConvertFrom-Json -InputObject $line -AsHashtable
        if (-not $event.type -or -not ($event.data -is [System.Collections.IDictionary])) {
            continue
        }
        if ($event.timestamp -is [DateTime]) { $event.timestamp = $event.timestamp.ToString('o') }
        if ($event.agentId -or $event.data.parentAgentTaskId) { continue }
        switch ($event.type) {
            'session.start' {
                $sawSdkEvent = $true
                $mode = $null
                $candidate = $null
                $task = $null
                $startedAt = $null
            }
            'session.resume' {
                $candidate = $null
                $task = $null
                $startedAt = $null
            }
            'session.mode_changed' {
                $sawSdkEvent = $true
                $mode = $event.data.newMode
                if ($mode -ne 'autopilot') { $candidate = $null }
            }
            'user.message' {
                if (-not $event.data.isAutopilotContinuation -and
                    $event.data.source -notmatch '^(system|agent-|skill-|notification|autopilot)') {
                    $candidate = $null
                    $task = $event.data.content
                    $startedAt = $event.timestamp
                    if ($event.data.agentMode) {
                        $sawSdkEvent = $true
                        $mode = $event.data.agentMode
                    }
                }
            }
            'session.task_complete' {
                $candidate = $null
                if ($mode -eq 'autopilot' -and $event.data.success -ne $false -and
                    (-not $event.data.outcome -or $event.data.outcome -eq 'completed') -and
                    $event.id -and -not [string]::IsNullOrWhiteSpace($event.data.summary)) {
                    $candidate = @{
                        eventId = $event.id
                        summary = $event.data.summary
                        task = $task
                        timestamp = $event.timestamp
                        startedAt = $startedAt
                    }
                }
            }
        }
    }
    if (-not $sawSdkEvent) {
        throw 'Unsupported transcript: SDK mode and task-completion events are required.'
    }
    return $candidate
}

function Get-EmailSummary {
    param([Parameter(Mandatory)][string]$Summary)

    $lines = [Collections.Generic.List[string]]::new()
    $inCode = $false
    foreach ($line in ($Summary -split '\r?\n')) {
        if ($line -match '^\s*(```|~~~)') { $inCode = -not $inCode; continue }
        if ($inCode) { continue }
        $text = ($line -replace '^\s*(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+)', '').Trim()
        if (-not $text) { continue }
        if ($text -match '(?i)(api[_ -]?key|access[_ -]?token|password|secret|connection[_ -]?string|authorization\s*:|bearer\s+|gh[pousr]_|github_pat_|sk-[a-z0-9]|sig=|-----BEGIN)') {
            continue
        }
        $text = $text -replace '[\x00-\x08\x0b\x0c\x0e-\x1f]', ''
        if ($text.Length -gt 240) { $text = $text.Substring(0, 237) + '...' }
        $lines.Add($text)
        if ($lines.Count -eq 2) { break }
    }
    if (-not $lines.Count) { throw 'No safe nonempty summary lines are available.' }
    return $lines -join "`n"
}

function Get-OperationId {
    param([Parameter(Mandatory)][string]$EventKey)

    $bytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($EventKey))
    return [Guid]::new([byte[]]$bytes[0..15]).ToString()
}

function Get-ProjectLabel {
    param([Parameter(Mandatory)][string]$WorkingDirectory)

    $directory = Get-Item -LiteralPath $WorkingDirectory -ErrorAction Stop
    if (-not $directory.PSIsContainer) { throw 'The hook working directory is not a directory.' }
    $projectPath = $directory.FullName
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitRoot = & git -C $projectPath rev-parse --show-toplevel 2>$null
        if ($LASTEXITCODE -eq 0 -and $gitRoot) { $projectPath = $gitRoot }
    }
    $label = Split-Path ($projectPath.TrimEnd('\', '/')) -Leaf
    if (-not $label) { throw 'Cannot determine a project name from the hook working directory.' }
    return $label
}

function Get-NotificationContent {
    param(
        [Parameter(Mandatory)][string]$Project,
        [Parameter(Mandatory)][string]$Task,
        [Parameter(Mandatory)][string]$Summary
    )

    $projectLabel = (Get-EmailSummary -Summary $Project) -split '\n' | Select-Object -First 1
    $taskLabel = (Get-EmailSummary -Summary $Task) -split '\n' | Select-Object -First 1
    if ($projectLabel.Length -gt 80) { $projectLabel = $projectLabel.Substring(0, 77) + '...' }
    if ($taskLabel.Length -gt 120) { $taskLabel = $taskLabel.Substring(0, 117) + '...' }
    $result = (Get-EmailSummary -Summary $Summary) -replace '\n', ' '
    if ($result.Length -gt 240) { $result = $result.Substring(0, 237) + '...' }
    return @{
        subject = "[Copilot] $projectLabel | $taskLabel"
        body = "Project: $projectLabel | Task: $taskLabel`nResult: $result"
    }
}

function Get-NotifierConfig {
    param([Parameter(Mandatory)][string]$Path)

    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable
    foreach ($name in @('subscriptionId', 'endpoint', 'sender', 'recipient')) {
        if ([string]::IsNullOrWhiteSpace($config[$name])) {
            throw "Missing notifier configuration field: $name"
        }
    }
    $endpoint = [Uri]$config.endpoint
    if ($endpoint.Scheme -ne 'https' -or
        $endpoint.Host -notmatch '^[a-z0-9-]+(?:\.[a-z0-9-]+)?\.communication\.azure\.com$' -or
        $endpoint.AbsolutePath -ne '/' -or $endpoint.Query -or $endpoint.UserInfo -or
        -not $endpoint.IsDefaultPort) {
        throw 'The notifier endpoint must be an HTTPS Azure Communication Services endpoint.'
    }
    foreach ($name in @('sender', 'recipient')) {
        $address = [Net.Mail.MailAddress]::new($config[$name])
        if ($address.Address -ne $config[$name]) { throw "Invalid $name address." }
    }
    return $config
}

function Send-CompletionEmail {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][string]$Summary,
        [string]$Subject = 'Copilot Autopilot completed',
        [Parameter(Mandatory)][string]$OperationId,
        [Parameter(Mandatory)][string]$ReceiptPath
    )

    $token = & az account get-access-token --subscription $Config.subscriptionId `
        --resource 'https://communication.azure.com' --query accessToken --output tsv --only-show-errors 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $token) {
        throw 'Azure authentication failed. Run az login and check the configured subscription.'
    }
    $headers = @{ Authorization = "Bearer $token" }
    $baseUri = $Config.endpoint.TrimEnd('/')
    $operationUri = "$baseUri/emails/operations/${OperationId}?api-version=2023-03-31"
    if (-not (Test-Path -LiteralPath $ReceiptPath)) {
        $body = @{
            senderAddress = $Config.sender
            recipients = @{ to = @(@{ address = $Config.recipient }) }
            content = @{ subject = $Subject; plainText = $Summary }
            userEngagementTrackingDisabled = $true
        } | ConvertTo-Json -Depth 5 -Compress
        # Persist uncertainty before POST so a retry polls instead of sending twice.
        @{ operationId = $OperationId; status = 'Pending'; updatedAt = [DateTime]::UtcNow.ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
        $headers['Operation-Id'] = $OperationId
        try {
            $null = Invoke-RestMethod -Method Post -Uri "$baseUri/emails:send?api-version=2023-03-31" `
                -Headers $headers -Body $body -ContentType 'application/json; charset=utf-8' -TimeoutSec 15
        }
        catch {
            throw 'ACS submission failed; status is uncertain. Inspect the operation before retrying.'
        }
        $headers.Remove('Operation-Id')
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        try {
            $result = Invoke-RestMethod -Method Get -Uri $operationUri -Headers $headers -TimeoutSec 10
        }
        catch {
            throw 'ACS operation lookup failed. The pending receipt is retained; email delivery is not confirmed.'
        }
        if ($result.status -eq 'Succeeded') {
            @{ operationId = $OperationId; status = 'Succeeded'; updatedAt = [DateTime]::UtcNow.ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
            return
        }
        if ($result.status -in @('Failed', 'Canceled')) {
            @{ operationId = $OperationId; status = $result.status; updatedAt = [DateTime]::UtcNow.ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
            throw 'ACS reported that the send operation did not succeed.'
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'ACS send is still pending. The pending receipt is retained; email delivery is not confirmed.'
}

function Get-KakaoNotificationContent {
    param(
        [Parameter(Mandatory)][string]$Project,
        [Parameter(Mandatory)][string]$Task,
        [Parameter(Mandatory)][string]$Summary,
        [Parameter(Mandatory)][string]$StartedAt,
        [Parameter(Mandatory)][string]$CompletedAt
    )

    foreach ($timestamp in @($StartedAt, $CompletedAt)) {
        if ($timestamp -notmatch '^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$') {
            throw 'Task timestamps must be ISO 8601 values with a timezone.'
        }
    }
    $start = [DateTimeOffset]::Parse($StartedAt, [Globalization.CultureInfo]::InvariantCulture)
    $end = [DateTimeOffset]::Parse($CompletedAt, [Globalization.CultureInfo]::InvariantCulture)
    if ($end -lt $start) { throw 'Task completion precedes its start.' }
    $elapsed = $end - $start
    $duration = '{0:00}:{1:00}:{2:00}' -f [Math]::Floor($elapsed.TotalHours), $elapsed.Minutes, $elapsed.Seconds
    $projectLabel = ((Get-EmailSummary $Project) -split '\n')[0]
    if ($projectLabel.Length -gt 20) { $projectLabel = $projectLabel.Substring(0, 17) + '...' }
    $taskLabel = ((Get-EmailSummary $Task) -split '\n')[0]
    $result = (Get-EmailSummary $Summary) -replace '\n', ' '
    $format = 'yyyy-MM-dd HH:mm:ss zzz'
    $metadata = "프로젝트: $projectLabel`n시작: $($start.ToLocalTime().ToString($format))`n완료: $($end.ToLocalTime().ToString($format))`n소요: $duration"
    $prefix = "$metadata`n요청 요약: "
    $separator = "`n결과 요약: "
    # Kakao's default text template displays at most 200 characters.
    $budget = 200 - $prefix.Length - $separator.Length
    $taskLimit = [int][Math]::Floor($budget / 2)
    $resultLimit = $budget - $taskLimit
    if ($taskLimit -lt 4) { throw 'Task metadata exceeds the Kakao text template limit.' }
    if ($taskLabel.Length -gt $taskLimit) { $taskLabel = $taskLabel.Substring(0, $taskLimit - 3) + '...' }
    if ($result.Length -gt $resultLimit) { $result = $result.Substring(0, $resultLimit - 3) + '...' }
    return "$prefix$taskLabel$separator$result"
}

function Get-KakaoConfig {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw 'Kakao is not configured. Run scripts\Connect-Kakao.ps1 first.'
    }
    $config = Import-Clixml -LiteralPath $Path
    foreach ($name in @('clientId', 'clientSecret', 'accessToken', 'refreshToken')) {
        if ($config[$name] -isnot [Security.SecureString] -or $config[$name].Length -eq 0) {
            throw "Missing encrypted Kakao configuration field: $name. Run Connect-Kakao.ps1 again."
        }
    }
    $url = [Uri]$config.messageUrl
    if (-not $url.IsAbsoluteUri -or $url.Scheme -ne 'https' -or $url.UserInfo) {
        throw 'The Kakao message link must be an absolute HTTPS URL registered with your app.'
    }
    $null = [DateTimeOffset]::Parse($config.expiresAt, [Globalization.CultureInfo]::InvariantCulture)
    return $config
}

function Get-PlainToken {
    param([Parameter(Mandatory)][Security.SecureString]$Value)
    return [Net.NetworkCredential]::new('', $Value).Password
}

function Save-KakaoTokens {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)]$Response,
        [Parameter(Mandatory)][string]$Path
    )

    if (-not $Response.access_token -or $Response.expires_in -le 0) {
        throw 'Kakao returned an invalid token response. Run Connect-Kakao.ps1 again.'
    }
    $Config.accessToken = ConvertTo-SecureString $Response.access_token -AsPlainText -Force
    $Config.expiresAt = [DateTimeOffset]::UtcNow.AddSeconds($Response.expires_in).ToString('o')
    if ($Response.refresh_token) {
        $Config.refreshToken = ConvertTo-SecureString $Response.refresh_token -AsPlainText -Force
    }
    if ($Config.refreshToken -isnot [Security.SecureString]) {
        throw 'Kakao did not issue a refresh token. Run Connect-Kakao.ps1 again.'
    }
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $Config | Export-Clixml -LiteralPath $temporary
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Send-CompletionKakao {
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][string]$OperationId,
        [Parameter(Mandatory)][string]$ReceiptPath
    )

    if ($Text.Length -gt 200) { throw 'Kakao message exceeds the 200-character display limit.' }
    if (Test-Path -LiteralPath $ReceiptPath) {
        $receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
        if ($receipt.status -eq 'Succeeded') { return }
        throw 'A prior Kakao send failed or is uncertain. Inspect the receipt and chat before retrying.'
    }
    $tokenLock = $null
    try {
        try {
            $tokenLock = [IO.File]::Open("$ConfigPath.lock", [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        }
        catch [IO.IOException] {
            throw 'Kakao configuration is busy or unavailable. No message was submitted.'
        }
        $config = Get-KakaoConfig $ConfigPath
        if ([DateTimeOffset]::Parse($config.expiresAt) -le [DateTimeOffset]::UtcNow.AddMinutes(1)) {
            $form = @{
                grant_type = 'refresh_token'
                client_id = Get-PlainToken $config.clientId
                client_secret = Get-PlainToken $config.clientSecret
                refresh_token = Get-PlainToken $config.refreshToken
            }
            try {
                $response = Invoke-RestMethod -Method Post -Uri 'https://kauth.kakao.com/oauth/token' `
                    -Body $form -ContentType 'application/x-www-form-urlencoded' -TimeoutSec 15
            }
            catch {
                throw 'Kakao token refresh failed. Check connectivity or run Connect-Kakao.ps1 again.'
            }
            Save-KakaoTokens -Config $config -Response $response -Path $ConfigPath
        }
        $token = Get-PlainToken $config.accessToken
    }
    finally {
        if ($tokenLock) { $tokenLock.Dispose() }
    }
    $template = @{
        object_type = 'text'
        text = $Text
        link = @{ web_url = $config.messageUrl; mobile_web_url = $config.messageUrl }
        button_title = '프로젝트 보기'
    } | ConvertTo-Json -Depth 4 -Compress
    # Kakao has no idempotency key or send-status lookup. Never blindly retry a POST.
    @{ operationId = $OperationId; provider = 'kakao'; status = 'Pending'; updatedAt = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
    try {
        $response = Invoke-RestMethod -Method Post -Uri 'https://kapi.kakao.com/v2/api/talk/memo/default/send' `
            -Headers @{ Authorization = "Bearer $token" } -Body @{ template_object = $template } `
            -ContentType 'application/x-www-form-urlencoded; charset=utf-8' -TimeoutSec 15
    }
    catch {
        throw 'Kakao submission failed; delivery is uncertain. Check your chat before deleting the pending receipt.'
    }
    $status = if ($null -ne $response.result_code -and $response.result_code -eq 0) { 'Succeeded' } else { 'Failed' }
    @{ operationId = $OperationId; provider = 'kakao'; status = $status; updatedAt = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $ReceiptPath -Encoding utf8
    if ($status -ne 'Succeeded') { throw 'Kakao did not confirm a successful send. Inspect your app permissions and quota.' }
}

Export-ModuleMember -Function Get-CompletionCandidate, Get-EmailSummary, Get-OperationId, Get-ProjectLabel, Get-NotificationContent, Get-NotifierConfig, Send-CompletionEmail, Get-KakaoNotificationContent, Get-KakaoConfig, Save-KakaoTokens, Send-CompletionKakao
