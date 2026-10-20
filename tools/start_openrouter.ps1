<#
Start the local synthetic app with the reviewed low-cost OpenRouter preset.
Without -PromptForKey an absent key stays disconnected. No call on startup.
Use -PromptForKey ONLY when ready: input is hidden, held in the current process
environment, and never saved by this script. Do not paste the key into chat.
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Database = 'work/openrouter-demo.sqlite',
    [string]$Model = '',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [switch]$PromptForKey,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $sourceRoot '.venv/Scripts/python.exe'
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { 'python' }
}
$env:CASHFLOW_MODEL_BASE_URL = 'https://openrouter.ai/api/v1'
if ($Model) { $env:CASHFLOW_MODEL_NAME = $Model }
elseif (-not $env:CASHFLOW_MODEL_NAME) { $env:CASHFLOW_MODEL_NAME = 'qwen/qwen3.7-flash' }
if ($PromptForKey) {
    $secureKey = Read-Host 'OpenRouter key (hidden; local process only)' -AsSecureString
    $keyPointer = [IntPtr]::Zero
    try {
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $env:CASHFLOW_MODEL_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    } finally {
        if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
        $secureKey.Dispose()
    }
}
Push-Location -LiteralPath $sourceRoot
try {
    if ($CheckOnly) {
        & $Python -m tools.openrouter_ready
    } else {
        Write-Host "Local synthetic app: http://127.0.0.1:$Port/agent"
        Write-Host 'Startup makes no model call. Verify connection and confirmed live questions may incur charges.'
        & $Python -m cashflow_copilot serve --db $Database --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw 'The local app or configuration check failed. Review the locally authored status.' }
} finally {
    Pop-Location
}
