#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
$temp = Join-Path ([IO.Path]::GetTempPath()) ("copilot-notifier-tests-" + [Guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $temp
$transcript = Join-Path $temp 'events.jsonl'
$count = 0

function Assert-True {
    param([bool]$Condition, [string]$Name)
    if (-not $Condition) { throw "FAILED: $Name" }
    $script:count++
}

function Write-Events {
    param([object[]]$Events)
    $Events | ForEach-Object { $_ | ConvertTo-Json -Depth 8 -Compress } |
        Set-Content -LiteralPath $transcript -Encoding utf8
}

try {
    $start = @{ type = 'session.start'; data = @{} }
    $auto = @{ type = 'session.mode_changed'; data = @{ newMode = 'autopilot' } }
    $complete = @{ type = 'session.task_complete'; id = 'done-1'; data = @{ summary = 'Implemented the requested change.'; success = $true; outcome = 'completed' } }
    Write-Events @($start, $auto, $complete)
    Assert-True ((Get-CompletionCandidate $transcript).eventId -eq 'done-1') 'Accepted Autopilot completion'
    foreach ($mode in @('interactive', 'plan', 'unknown')) {
        Write-Events @($start, @{ type = 'session.mode_changed'; data = @{ newMode = $mode } }, $complete)
        Assert-True ($null -eq (Get-CompletionCandidate $transcript)) "Exclude $mode"
    }
    Write-Events @($start, $complete)
    Assert-True ($null -eq (Get-CompletionCandidate $transcript)) 'Missing mode fails closed'
    Write-Events @($start, $auto, $complete, @{ type = 'session.mode_changed'; data = @{ newMode = 'interactive' } })
    Assert-True ($null -eq (Get-CompletionCandidate $transcript)) 'Mode switch disables notification'
    foreach ($outcome in @('continue', 'blocked')) {
        Write-Events @($start, $auto, @{ type = 'session.task_complete'; id = 'rejected'; data = @{ summary = 'Not done'; outcome = $outcome; success = $false } })
        Assert-True ($null -eq (Get-CompletionCandidate $transcript)) "Exclude $outcome"
    }
    Write-Events @($start, $auto, @{ type = 'session.task_complete'; agentId = 'child'; id = 'child-done'; data = @{ summary = 'Child finished' } })
    Assert-True ($null -eq (Get-CompletionCandidate $transcript)) 'Exclude subagent'
    Write-Events @($start, $auto, $complete, @{ type = 'user.message'; data = @{ content = 'Next task' } })
    Assert-True ($null -eq (Get-CompletionCandidate $transcript)) 'Do not resend prior request'
    Write-Events @($start, $auto, $complete, @{ type = 'session.resume'; data = @{} })
    Assert-True ($null -eq (Get-CompletionCandidate $transcript)) 'Do not resend on resume'
    $request = @{ type = 'user.message'; data = @{ content = "Add completion emails`nLonger task details"; agentMode = 'autopilot' } }
    Write-Events @($start, $request, $complete)
    $candidate = Get-CompletionCandidate $transcript
    Assert-True ($candidate.eventId -eq 'done-1') 'Initial SDK user-message mode is authoritative'
    Assert-True ($candidate.task -eq $request.data.content) 'Capture current human task'
    Write-Events @($start, $request, @{ type = 'user.message'; data = @{ content = 'Continue'; isAutopilotContinuation = $true } }, $complete)
    Assert-True ((Get-CompletionCandidate $transcript).task -eq $request.data.content) 'Automatic continuation keeps human title'
    Write-Events @($start, $request, @{ type = 'user.message'; data = @{ content = 'System notification'; source = 'system' } }, $complete)
    Assert-True ((Get-CompletionCandidate $transcript).task -eq $request.data.content) 'System notification keeps human title'
    foreach ($mode in @('interactive', 'plan')) {
        Write-Events @($start, @{ type = 'user.message'; data = @{ content = 'Do a task'; agentMode = $mode } }, $complete)
        Assert-True ($null -eq (Get-CompletionCandidate $transcript)) "Exclude SDK user-message $mode mode"
    }
    Write-Events @(@{ role = 'assistant'; content = 'Done' })
    $rejected = $false
    try { $null = Get-CompletionCandidate $transcript } catch { $rejected = $true }
    Assert-True $rejected 'Unsupported Local transcript fails closed'
    $summary = Get-EmailSummary "First result.`nSecond result.`nThird result."
    Assert-True ($summary -eq "First result.`nSecond result.") 'Exactly two summary lines'
    $summary = Get-EmailSummary ('x' * 300)
    Assert-True ($summary.Length -eq 240) 'Summary length bound'
    $summary = Get-EmailSummary "api_key=example`nSafe result."
    Assert-True ($summary -eq 'Safe result.') 'Exclude credential-like line'
    $summary = Get-EmailSummary "``````text`nHidden code`n```````nVisible result."
    Assert-True ($summary -eq 'Visible result.') 'Exclude fenced code'
    Assert-True ((Get-OperationId 'session:event') -eq (Get-OperationId 'session:event')) 'Stable operation identity'
    Assert-True ((Get-OperationId 'session:event') -ne (Get-OperationId 'session:other')) 'Distinct completion identity'
    $content = Get-NotificationContent -Project 'tools' -Task "Add completion emails`nDo not include full prompt" `
        -Summary "Azure email is ready.`nAutopilot notifications are enabled.`nDo not include extra details."
    Assert-True ($content.subject -eq '[Copilot] tools | Add completion emails') 'Subject identifies project and task'
    Assert-True (($content.body -split '\n').Count -eq 2) 'Metadata and result fit exactly two lines'
    Assert-True ($content.body.StartsWith('Project: tools | Task: Add completion emails')) 'Body identifies project and task'
    Assert-True ($content.body.Contains('Result: Azure email is ready. Autopilot notifications are enabled.')) 'Body preserves result summary'
    Assert-True (-not $content.body.Contains('full prompt')) 'Do not send full human prompt'
    Assert-True ((Get-ProjectLabel -WorkingDirectory (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)) -eq 'tools') 'Resolve Git project root name'
    Assert-True ((Get-ProjectLabel -WorkingDirectory $temp) -eq (Split-Path $temp -Leaf)) 'Resolve non-Git folder name'
    $configPath = Join-Path $temp 'config.json'
    foreach ($endpoint in @('http://example.communication.azure.com', 'https://example.com', 'https://example.communication.azure.com.attacker.test')) {
        @{ subscriptionId = 'test'; sender = 'sender@example.test'; recipient = 'recipient@example.test'; endpoint = $endpoint } |
            ConvertTo-Json | Set-Content -LiteralPath $configPath
        $rejected = $false
        try { $null = Get-NotifierConfig $configPath } catch { $rejected = $true }
        Assert-True $rejected "Reject unsafe endpoint: $endpoint"
    }
    foreach ($endpoint in @('https://example.communication.azure.com', 'https://example.unitedstates.communication.azure.com')) {
        @{ subscriptionId = 'test'; sender = 'sender@example.test'; recipient = 'recipient@example.test'; endpoint = $endpoint } |
            ConvertTo-Json | Set-Content -LiteralPath $configPath
        Assert-True ((Get-NotifierConfig $configPath).endpoint -eq $endpoint) 'Accept Azure endpoint'
    }
    $module = Get-Module Notifier
    & $module {
        $script:testPosts = 0
        $script:testGets = 0
        function script:az {
            $global:LASTEXITCODE = 0
            'test-token'
        }
        function script:Invoke-RestMethod {
            param($Method, $Uri, $Headers, $Body, $ContentType, $TimeoutSec)
            if ($Method -eq 'Post') {
                $script:testPosts++
                return @{ status = 'Running' }
            }
            $script:testGets++
            return @{ status = 'Succeeded' }
        }
    }
    $receiptPath = Join-Path $temp 'receipt.json'
    $config = @{ subscriptionId = 'test'; endpoint = 'https://example.communication.azure.com'; sender = 'sender@example.test'; recipient = 'recipient@example.test' }
    $operationId = Get-OperationId 'offline:test'
    Send-CompletionEmail -Config $config -Summary 'Completed.' -OperationId $operationId -ReceiptPath $receiptPath
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Assert-True ($receipt.status -eq 'Succeeded') 'Persist only confirmed send success'
    Send-CompletionEmail -Config $config -Summary 'Completed.' -OperationId $operationId -ReceiptPath $receiptPath
    Assert-True ((& $module { $script:testPosts }) -eq 1) 'Do not POST a duplicate operation'
    Assert-True ((& $module { $script:testGets }) -eq 2) 'Poll persisted operation on retry'
    Assert-True (-not (Get-Content -LiteralPath $receiptPath -Raw).Contains('Completed.')) 'Do not store summary in receipt'
    Remove-Item -LiteralPath $receiptPath
    & $module {
        function script:Invoke-RestMethod {
            param($Method, $Uri, $Headers, $Body, $ContentType, $TimeoutSec)
            if ($Method -eq 'Post') { return @{ status = 'Running' } }
            return @{ status = 'Failed' }
        }
    }
    $rejected = $false
    try {
        Send-CompletionEmail -Config $config -Summary 'Completed.' -OperationId $operationId -ReceiptPath $receiptPath
    }
    catch { $rejected = $true }
    Assert-True $rejected 'Surface failed ACS operation'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Assert-True ($receipt.status -eq 'Failed') 'Never mark failed ACS operation as sent'
    Remove-Item -LiteralPath $receiptPath
    & $module {
        function script:Invoke-RestMethod {
            param($Method, $Uri, $Headers, $Body, $ContentType, $TimeoutSec)
            throw 'Simulated uncertain network failure'
        }
    }
    $rejected = $false
    try {
        Send-CompletionEmail -Config $config -Summary 'Completed.' -OperationId $operationId -ReceiptPath $receiptPath
    }
    catch { $rejected = $true }
    Assert-True $rejected 'Surface uncertain network failure'
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Assert-True ($receipt.status -eq 'Pending') 'Keep uncertain submission pending'
    foreach ($mode in @('interactive', 'plan')) {
        Write-Events @($start, @{ type = 'session.mode_changed'; data = @{ newMode = $mode } }, $complete)
        $hook = @{ hook_event_name = 'Stop'; session_id = 'offline-test'; transcript_path = $transcript } |
            ConvertTo-Json -Compress
        $hook | & (Get-Command pwsh).Source -NoProfile -NonInteractive `
            -File (Join-Path $PSScriptRoot 'Notify-Completion.ps1') -ConfigPath (Join-Path $temp 'missing-config.json')
        Assert-True ($LASTEXITCODE -eq 0) "Hook exits without configuring or sending email in $mode mode"
    }
    $request.timestamp = '2026-10-16T09:00:00Z'
    $complete.timestamp = '2026-10-16T09:01:05Z'
    Write-Events @($start, $request, @{
        type = 'user.message'; timestamp = '2026-10-16T09:00:30Z'
        data = @{ content = 'Continue'; isAutopilotContinuation = $true }
    }, $complete)
    $candidate = Get-CompletionCandidate $transcript
    Assert-True ([DateTimeOffset]::Parse($candidate.startedAt) -eq [DateTimeOffset]::Parse($request.timestamp)) 'Start time comes from human request, not continuation'
    Assert-True ([DateTimeOffset]::Parse($candidate.timestamp) -eq [DateTimeOffset]::Parse($complete.timestamp)) 'Completion time comes from accepted event'
    $newRequest = @{
        type = 'user.message'; timestamp = '2026-10-16T09:00:45Z'
        data = @{ content = 'Updated task' }
    }
    Write-Events @($start, $request, $newRequest, $complete)
    Assert-True ([DateTimeOffset]::Parse((Get-CompletionCandidate $transcript).startedAt) -eq [DateTimeOffset]::Parse($newRequest.timestamp)) 'New human request resets task start'
    $content = Get-KakaoNotificationContent -Project 'tools' -Task "Add Kakao notifications`nPrivate full request" `
        -Summary "Kakao notifications are ready.`nTiming is included." `
        -StartedAt $candidate.startedAt -CompletedAt $candidate.timestamp
    Assert-True (($content -split '\n').Count -eq 6) 'Kakao contains exactly six fields'
    foreach ($label in @('프로젝트:', '시작:', '완료:', '소요:', '요청 요약:', '결과 요약:')) {
        Assert-True ($content.Contains($label)) "Kakao includes $label"
    }
    Assert-True ($content.Contains('00:01:05')) 'Elapsed duration is accurate'
    Assert-True ($content.Contains(([DateTimeOffset]::Parse($request.timestamp).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss zzz')))) 'Start displays local time and timezone'
    Assert-True (-not $content.Contains('Private')) 'Kakao excludes full prompt'
    $longContent = Get-KakaoNotificationContent -Project ('p' * 200) -Task ('t' * 300) -Summary ('r' * 300) `
        -StartedAt '2026-10-16T09:00:00Z' -CompletedAt '2026-10-18T10:01:05Z'
    Assert-True ($longContent.Length -le 200) 'All six fields fit the actual Kakao 200-character limit'
    Assert-True ($longContent.Contains('49:01:05')) 'Duration does not wrap after 24 hours'
    foreach ($times in @(
        @('', '2026-10-16T09:01:05Z'),
        @('invalid', '2026-10-16T09:01:05Z'),
        @('2026-10-16T09:00:00', '2026-10-16T09:01:05Z'),
        @('2026-10-16T09:02:00Z', '2026-10-16T09:01:05Z')
    )) {
        $rejected = $false
        try {
            $null = Get-KakaoNotificationContent -Project 'tools' -Task 'Task' -Summary 'Result' `
                -StartedAt $times[0] -CompletedAt $times[1]
        }
        catch { $rejected = $true }
        Assert-True $rejected 'Missing, invalid, ambiguous, or reversed timestamps fail explicitly'
    }
    $kakaoPath = Join-Path $temp 'kakao.xml'
    $kakaoReceipt = Join-Path $temp 'kakao-receipt.json'
    $kakaoConfig = @{
        clientId = ConvertTo-SecureString 'offline-app-key' -AsPlainText -Force
        clientSecret = ConvertTo-SecureString 'offline-client-secret' -AsPlainText -Force
        messageUrl = 'https://example.test/project'
    }
    Save-KakaoTokens -Config $kakaoConfig -Response @{
        access_token = 'offline-access'; refresh_token = 'offline-refresh'; expires_in = 3600
    } -Path $kakaoPath
    Assert-True ((Get-KakaoConfig $kakaoPath).accessToken -is [Security.SecureString]) 'Kakao tokens persist as encrypted SecureStrings'
    Assert-True (-not (Get-Content -LiteralPath $kakaoPath -Raw).Contains('offline-refresh')) 'Stored config does not contain plaintext refresh token'
    & $module {
        $script:kakaoPosts = 0
        $script:kakaoRefreshes = 0
        $script:kakaoMode = 'success'
        function script:Invoke-RestMethod {
            param($Method, $Uri, $Headers, $Body, $ContentType, $TimeoutSec)
            if ($Uri -eq 'https://kauth.kakao.com/oauth/token') {
                $script:kakaoRefreshes++
                if ($Body.grant_type -ne 'refresh_token' -or $Body.refresh_token -ne 'offline-refresh') {
                    throw 'Incorrect refresh request'
                }
                return @{ access_token = 'renewed-access'; refresh_token = 'rotated-refresh'; expires_in = 3600 }
            }
            if ($Uri -ne 'https://kapi.kakao.com/v2/api/talk/memo/default/send' -or $Method -ne 'Post') {
                throw 'Unexpected Kakao endpoint'
            }
            $script:kakaoPosts++
            $script:kakaoTemplate = $Body.template_object | ConvertFrom-Json -AsHashtable
            $script:kakaoAuthorization = $Headers.Authorization
            if ($script:kakaoMode -eq 'network') { throw 'Simulated network failure' }
            if ($script:kakaoMode -eq 'failed') { return @{ result_code = -1 } }
            if ($script:kakaoMode -eq 'missing') { return @{} }
            return @{ result_code = 0 }
        }
    }
    Send-CompletionKakao -ConfigPath $kakaoPath -Text $content -OperationId 'kakao-offline' -ReceiptPath $kakaoReceipt
    Assert-True ((Get-Content -LiteralPath $kakaoReceipt -Raw | ConvertFrom-Json).status -eq 'Succeeded') 'Kakao marks only confirmed sends successful'
    $template = & $module { $script:kakaoTemplate }
    Assert-True ($template.object_type -eq 'text' -and $template.text -eq $content) 'Kakao receives complete six-field template'
    Assert-True ($template.link.web_url -eq $kakaoConfig.messageUrl) 'Kakao uses registered project URL'
    Send-CompletionKakao -ConfigPath $kakaoPath -Text $content -OperationId 'kakao-offline' -ReceiptPath $kakaoReceipt
    Assert-True ((& $module { $script:kakaoPosts }) -eq 1) 'Successful Kakao send is never duplicated'
    Assert-True (-not (Get-Content -LiteralPath $kakaoReceipt -Raw).Contains('tools')) 'Kakao receipts exclude message content'
    Remove-Item -LiteralPath $kakaoReceipt
    $kakaoConfig.expiresAt = [DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o')
    $kakaoConfig | Export-Clixml -LiteralPath $kakaoPath
    Send-CompletionKakao -ConfigPath $kakaoPath -Text $content -OperationId 'kakao-refresh' -ReceiptPath $kakaoReceipt
    Assert-True ((& $module { $script:kakaoRefreshes }) -eq 1) 'Expired access token is refreshed automatically'
    Assert-True ((& $module { $script:kakaoAuthorization }) -eq 'Bearer renewed-access') 'Send uses refreshed token'
    $stored = Get-KakaoConfig $kakaoPath
    Assert-True ([Net.NetworkCredential]::new('', $stored.refreshToken).Password -eq 'rotated-refresh') 'Rotated refresh token is saved'
    Remove-Item -LiteralPath $kakaoReceipt
    foreach ($mode in @('network', 'failed', 'missing')) {
        & $module { param($Mode) $script:kakaoMode = $Mode } $mode
        $rejected = $false
        try {
            Send-CompletionKakao -ConfigPath $kakaoPath -Text $content -OperationId "kakao-$mode" -ReceiptPath $kakaoReceipt
        }
        catch { $rejected = $true }
        Assert-True $rejected "Surface Kakao $mode failure"
        $expectedStatus = if ($mode -eq 'network') { 'Pending' } else { 'Failed' }
        Assert-True ((Get-Content -LiteralPath $kakaoReceipt -Raw | ConvertFrom-Json).status -eq $expectedStatus) "Retain $expectedStatus receipt"
        $posts = & $module { $script:kakaoPosts }
        $rejected = $false
        try {
            Send-CompletionKakao -ConfigPath $kakaoPath -Text $content -OperationId "kakao-$mode" -ReceiptPath $kakaoReceipt
        }
        catch { $rejected = $true }
        Assert-True ($rejected -and (& $module { $script:kakaoPosts }) -eq $posts) 'Failed or uncertain Kakao sends require manual inspection, not retries'
        Remove-Item -LiteralPath $kakaoReceipt
    }
    Write-Output "$count offline checks passed."
}
finally {
    foreach ($file in @($transcript, (Join-Path $temp 'config.json'), (Join-Path $temp 'receipt.json'),
        (Join-Path $temp 'kakao.xml'), (Join-Path $temp 'kakao.xml.lock'), (Join-Path $temp 'kakao-receipt.json'))) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
    Remove-Item -LiteralPath $temp
}
