<#
Run the full synthetic matrix using credentials already in this terminal's
environment. No key prompt/file or access to the running app's environment.
Completed cases are checkpointed. -StartApp restarts the local app afterward,
including when individual live cases fail. The database is never reset.
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Output = 'work/live-0.17.4/evaluation.json',
    [string]$Database = 'work/openrouter-demo.sqlite',
    [string[]]$Case = @(),
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [switch]$StartApp
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $sourceRoot '.venv/Scripts/python.exe'
    $Python = if (Test-Path -LiteralPath $localPython) { $localPython } else { 'python' }
}
foreach ($name in @('CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME')) {
    if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
        throw "Missing $name in this terminal. Configure it locally; do not paste credentials into chat."
    }
}
Push-Location -LiteralPath $sourceRoot
try {
    if (Test-Path -LiteralPath $Output) { throw 'Report already exists. Choose another -Output path to preserve previous results.' }
    Write-Host 'Runs selected cases, or all 23 isolated synthetic live cases. Provider charges may apply. No demo database is used.'
    $evaluationArguments = @('-m', 'tools.verify_live', '--output', $Output)
    foreach ($caseId in $Case) { $evaluationArguments += @('--case', $caseId) }
    & $Python @evaluationArguments
    $evaluationCode = $LASTEXITCODE
    Write-Host "Evaluation exit code: $evaluationCode. Read the report; commentary/refusal review remains separate."
    if ($StartApp) {
        & $Python -m cashflow_copilot serve --db $Database --port $Port
    }
} finally {
    Pop-Location
}
