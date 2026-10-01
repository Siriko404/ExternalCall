$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'run_all.py')
if ($LASTEXITCODE -ne 0) { throw 'Required checks failed or are unavailable.' }
