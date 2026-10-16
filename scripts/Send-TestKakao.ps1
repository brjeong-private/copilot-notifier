#Requires -Version 7.0
param([string]$ConfigPath = (Join-Path $PSScriptRoot '..\.local\kakao.xml'))

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
$null = Get-KakaoConfig $ConfigPath
$receipts = Join-Path (Split-Path $ConfigPath -Parent) 'kakao-receipts'
$null = New-Item -ItemType Directory -Path $receipts -Force
$operationId = [Guid]::NewGuid().ToString()
$now = [DateTimeOffset]::UtcNow
$content = Get-KakaoNotificationContent -Project 'tools' -Task '카카오톡 알림 연결 확인' `
    -Summary '작업 완료 알림을 나와의 채팅으로 보냅니다.' `
    -StartedAt $now.AddSeconds(-5).ToString('o') -CompletedAt $now.ToString('o')
Send-CompletionKakao -ConfigPath $ConfigPath -Text $content -OperationId $operationId `
    -ReceiptPath (Join-Path $receipts "$operationId.json")
Write-Output 'Kakao confirmed the test send. Check your My Chat conversation.'
