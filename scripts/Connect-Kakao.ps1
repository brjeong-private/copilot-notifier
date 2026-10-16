#Requires -Version 7.0
param(
    [Parameter(Mandatory)][Uri]$MessageUrl,
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\.local\kakao.xml')
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This setup requires Windows DPAPI for encrypted token storage.' }
if (-not $MessageUrl.IsAbsoluteUri -or $MessageUrl.Scheme -ne 'https' -or $MessageUrl.UserInfo) {
    throw 'MessageUrl must be an HTTPS URL registered in your Kakao app web domains.'
}
Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
$clientId = Read-Host 'Kakao REST API key (hidden)' -AsSecureString
$clientSecret = Read-Host 'Kakao client secret (hidden)' -AsSecureString
if (-not $clientId.Length -or -not $clientSecret.Length) { throw 'Both app credentials are required.' }
$redirect = 'http://localhost:53682/'
$state = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$key = [Net.NetworkCredential]::new('', $clientId).Password
$authorize = 'https://kauth.kakao.com/oauth/authorize?response_type=code&scope=talk_message&client_id={0}&redirect_uri={1}&state={2}' -f `
    [Uri]::EscapeDataString($key), [Uri]::EscapeDataString($redirect), $state
$listener = [Net.HttpListener]::new()
$listener.Prefixes.Add($redirect)
$lock = $null
try {
    $null = New-Item -ItemType Directory -Path (Split-Path $ConfigPath -Parent) -Force
    $lock = [IO.File]::Open("$ConfigPath.lock", [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $listener.Start()
    Start-Process $authorize
    Write-Output 'Complete Kakao login and talk_message consent in your browser (3-minute timeout).'
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    $code = $null
    while (-not $code) {
        $pending = $listener.GetContextAsync()
        $remaining = [int][Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if (-not $pending.Wait($remaining)) { throw 'Kakao login timed out. Run this script again.' }
        $context = $pending.GetAwaiter().GetResult()
        $query = $context.Request.QueryString
        if ($context.Request.Url.AbsolutePath -ne '/' -or $query['state'] -cne $state) {
            $context.Response.StatusCode = 400
            $context.Response.Close()
            continue
        }
        if ($query['error'] -or -not $query['code']) {
            $context.Response.StatusCode = 400
            $context.Response.Close()
            throw 'Kakao authorization was denied or returned no code.'
        }
        $code = $query['code']
        $bytes = [Text.Encoding]::UTF8.GetBytes('Login received. Return to PowerShell to check setup.')
        $context.Response.ContentType = 'text/plain; charset=utf-8'
        $context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $context.Response.Close()
    }
    try {
        $tokens = Invoke-RestMethod -Method Post -Uri 'https://kauth.kakao.com/oauth/token' -Body @{
            grant_type = 'authorization_code'
            client_id = $key
            client_secret = [Net.NetworkCredential]::new('', $clientSecret).Password
            redirect_uri = $redirect
            code = $code
        } -ContentType 'application/x-www-form-urlencoded' -TimeoutSec 15
    }
    catch {
        throw 'Kakao token exchange failed. Check app credentials, redirect URI, and consent settings.'
    }
    $config = @{ clientId = $clientId; clientSecret = $clientSecret; messageUrl = $MessageUrl.AbsoluteUri }
    Save-KakaoTokens -Config $config -Response $tokens -Path $ConfigPath
    Write-Output 'Kakao credentials saved encrypted for this Windows user. Run Send-TestKakao.ps1 next.'
}
finally {
    $listener.Close()
    if ($lock) { $lock.Dispose() }
}
