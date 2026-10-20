param(
    [string]$Python = 'python',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [string]$Database = 'data/state.sqlite'
)
$ErrorActionPreference = 'Stop'
$fluxoraSource = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $fluxoraSource
try {
    Write-Host "Fluxora product website: http://127.0.0.1:$Port/welcome"
    Write-Host "Fluxora app workspace:  http://127.0.0.1:$Port/app"
    Write-Host "Documentation:         http://127.0.0.1:$Port/docs"
    Write-Host 'Synthetic local sandbox. Existing database is preserved. Ctrl+C stops the server.'
    & $Python -m cashflow_copilot serve --db $Database --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Fluxora exited with an error; inspect the local message above.' }
} finally { Pop-Location }
