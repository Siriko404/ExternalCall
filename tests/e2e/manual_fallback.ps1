[CmdletBinding()]
param([string]$Root = '')
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'manual_fallback.py'))
if ($Root) { $arguments += @($Root) }
& python @arguments
if ($LASTEXITCODE -ne 0) { throw 'manual_fallback failed' }
