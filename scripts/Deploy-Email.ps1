#Requires -Version 7.0
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$Recipient,
    [string]$ResourceGroup = 'rg-copilot-tools',
    [string]$Location = 'westus3'
)

$ErrorActionPreference = 'Stop'
$null = [Net.Mail.MailAddress]::new($Recipient)
$root = Split-Path $PSScriptRoot -Parent
$suffix = ($SubscriptionId -replace '-', '').Substring(0, 10)
$emailName = "copilot-tools-email-$suffix"
$acsName = "copilot-tools-acs-$suffix"
$null = & az group create --subscription $SubscriptionId --name $ResourceGroup --location $Location --only-show-errors --output none
if ($LASTEXITCODE -ne 0) { throw 'Could not create the dedicated resource group.' }
$output = & az deployment group create --subscription $SubscriptionId --resource-group $ResourceGroup `
    --name 'copilot-notifier-email' --mode Incremental --template-file (Join-Path $root 'infra\email.json') `
    --parameters "emailServiceName=$emailName" "communicationServiceName=$acsName" `
    --query properties.outputs --output json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Email infrastructure deployment failed.' }
$deployment = $output | ConvertFrom-Json
$acs = & az resource show --subscription $SubscriptionId --ids $deployment.communicationServiceId.value `
    --api-version 2023-03-31 --output json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Could not read the deployed communication resource.' }
$domain = & az resource show --subscription $SubscriptionId --ids $deployment.domainId.value `
    --api-version 2023-03-31 --output json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Could not read the managed email domain.' }
$acs = $acs | ConvertFrom-Json
$domain = $domain | ConvertFrom-Json
if ($acs.properties.provisioningState -ne 'Succeeded' -or
    $domain.properties.provisioningState -ne 'Succeeded' -or
    -not $domain.properties.mailFromSenderDomain) {
    throw 'The deployed email resources are not ready.'
}
$local = Join-Path $root '.local'
$null = New-Item -ItemType Directory -Path $local -Force
@{
    subscriptionId = $SubscriptionId
    resourceGroup = $ResourceGroup
    endpoint = "https://$($acs.properties.hostName)"
    sender = "DoNotReply@$($domain.properties.mailFromSenderDomain)"
    recipient = $Recipient
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $local 'config.json') -Encoding utf8
Import-Module (Join-Path $PSScriptRoot 'Notifier.psm1') -Force
$null = Get-NotifierConfig -Path (Join-Path $local 'config.json')
Write-Output 'Email resources are ready. Local configuration saved; no access keys were retrieved or stored.'
