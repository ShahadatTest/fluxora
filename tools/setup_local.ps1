[CmdletBinding()]
param(
    [string]$Python = '',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [string]$Database = '.runtime/local/state.sqlite',
    [ValidateSet('atlas','studio','retail')][string]$Example = 'atlas',
    [switch]$Telegram,
    [switch]$PromptForModelKey,
    [switch]$PromptForTelegramToken,
    [string]$TelegramOwnerId = '',
    [string]$TelegramBotName = '',
    [switch]$UseTelegramModel,
    [string]$ModelBaseUrl = '',
    [string]$ModelName = '',
    [switch]$CheckOnly,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$savedEnvironment = @{}
function Set-TemporaryEnvironment([string]$Name,[string]$Value) {
    if (-not $savedEnvironment.ContainsKey($Name)) { $savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name,'Process') }
    [Environment]::SetEnvironmentVariable($Name,$Value,'Process')
}
function Read-PrivateEnvironment([string]$Name,[string]$Label) {
    $secureValue = Read-Host $Label -AsSecureString
    $privatePointer = [IntPtr]::Zero
    try {
        $privatePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
        Set-TemporaryEnvironment $Name ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($privatePointer))
    } finally {
        if ($privatePointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($privatePointer) }
        $secureValue.Dispose()
    }
}
$pythonPrefix = @()
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py'; $pythonPrefix = @('-3') }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
    else { throw 'Install Python 3.10+ from python.org, then run START_FLUXORA.cmd again.' }
}
Push-Location -LiteralPath $sourceRoot
try {
    & $Python @pythonPrefix -c 'import sys;raise SystemExit(sys.version_info < (3,10))'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
    if ($PromptForModelKey) { Read-PrivateEnvironment 'CASHFLOW_MODEL_API_KEY' 'Model API key (hidden; never saved)' }
    if ($ModelBaseUrl) { Set-TemporaryEnvironment 'CASHFLOW_MODEL_BASE_URL' $ModelBaseUrl }
    if ($ModelName) { Set-TemporaryEnvironment 'CASHFLOW_MODEL_NAME' $ModelName }
    if ($PromptForTelegramToken) { Read-PrivateEnvironment 'FLUXORA_TELEGRAM_BOT_TOKEN' 'LOCAL bot token (hidden; do not use an actively polling VPS bot)' }
    if ($TelegramOwnerId) {
        if ($TelegramOwnerId -notmatch '^[1-9][0-9]{0,15}$') { throw 'Use your numeric Telegram user ID.' }
        Set-TemporaryEnvironment 'FLUXORA_TELEGRAM_OWNER_ID' $TelegramOwnerId
    }
    if ($TelegramBotName) { Set-TemporaryEnvironment 'FLUXORA_TELEGRAM_BOT_NAME' $TelegramBotName }
    if ($Telegram) {
        Set-TemporaryEnvironment 'FLUXORA_TELEGRAM_ENABLED' '1'
        Set-TemporaryEnvironment 'FLUXORA_TELEGRAM_USE_MODEL' $(if ($UseTelegramModel) { '1' } else { '0' })
    }
    $setupArgs = @('-m','tools.local_setup','--port',"$Port",'--db',$Database,'--example',$Example)
    if ($CheckOnly) { $setupArgs += '--check' }
    else {
        $setupArgs += '--start'
        if (-not $NoBrowser) { $setupArgs += '--open-browser' }
        if ($Telegram) { $setupArgs += '--telegram' }
    }
    & $Python @pythonPrefix @setupArgs
    if ($LASTEXITCODE -notin @(0,130)) { throw 'Local setup/app stopped with an error. Data was not reset. See docs/LOCAL-SETUP.md.' }
} finally {
    foreach ($name in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name,$savedEnvironment[$name],'Process') }
    $savedEnvironment.Clear()
    Pop-Location
}
