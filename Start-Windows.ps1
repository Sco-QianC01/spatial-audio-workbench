#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create this repository .venv with Windows Python before starting.'
}
& $python -I (Join-Path $PSScriptRoot 'run.py') @args
exit $LASTEXITCODE
