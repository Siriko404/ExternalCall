[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null

Push-Location $root
try {
    go build -o (Join-Path $bin 'externalcall-host.exe') ./cmd/nativehost
    if ($LASTEXITCODE -ne 0) {
        throw "Go build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Output (Join-Path $bin 'externalcall-host.exe')
