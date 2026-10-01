[CmdletBinding()]
param([string]$Root = '')
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'fixture_call.py'))
if ($Root) { $arguments += @($Root) }
& python @arguments
if ($LASTEXITCODE -ne 0) { throw 'fixture_call failed' }
