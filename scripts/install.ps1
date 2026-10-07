<#
.SYNOPSIS
Register the ExternalCall native-messaging host for one already-loaded extension.

.DESCRIPTION
The host manifest must name one exact extension ID in allowed_origins, with no
wildcard. That ID used to be typed in by hand off chrome://extensions, which
forced the extension to be loaded first and made a thirty-two character
transcription part of the install.

Neither is needed. Chrome derives an unpacked extension's ID from where it sits
on disk, so scripts\extension_id.py works it out: from Chrome's own profile data
when the extension is already loaded, and from the path itself when it is not.
Pass -ExtensionId to override both.

Everything is checked before anything is written. A missing prerequisite fails
here, with the reason, rather than surfacing later as a side panel that says the
companion is unavailable.

.PARAMETER ExtensionId
Optional. Overrides the resolved ID; use it only when chrome://extensions
disagrees with what this script reports.

.PARAMETER WhatIf
Report what would be done and change nothing.
#>
[CmdletBinding()]
param(
    [AllowEmptyString()]
    [string]$ExtensionId = '',
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$hostPath = Join-Path $root 'bin\externalcall-host.exe'
$templatePath = Join-Path $root 'native-host\com.sina.externalcall.template.json'
$manifestPath = Join-Path $root 'native-host\com.sina.externalcall.json'
$extensionPath = Join-Path $root 'extension'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.sina.externalcall'

function Fail([string]$problem, [string]$remedy) {
    throw "$problem`n  Fix: $remedy"
}

# ---------- preflight ----------

Write-Output 'Checking prerequisites.'

if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    Fail "The host manifest template is missing: $templatePath" 'Run this from a complete checkout of the repository.'
}
if (-not (Test-Path -LiteralPath (Join-Path $extensionPath 'manifest.json') -PathType Leaf)) {
    Fail "The extension directory is missing: $extensionPath" 'Run this from a complete checkout of the repository.'
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) {
    Fail 'Python was not found on PATH.' 'Install Python 3.10 or newer and ensure `python` runs in a new terminal. The registered host launches the Python companion through PATH.'
}
$pythonVersion = (& $python.Source --version | Out-String)
if ($pythonVersion -notmatch 'Python (\d+)\.(\d+)') {
    Fail "Could not read a version from ``$($python.Source) --version``: $pythonVersion" 'Ensure `python` on PATH is a real Python interpreter and not the Windows Store stub.'
}
if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) {
    Fail "Python $($Matches[1]).$($Matches[2]) is too old." 'Install Python 3.10 or newer.'
}
Write-Output "  Python $($Matches[1]).$($Matches[2]) at $($python.Source)"

# The ID is resolved here, after Python is known to work, because the resolver
# is Python. Chrome's own profile data wins when the extension is loaded; the
# path derivation covers the case where it is not, which is what lets the host
# be registered before anyone has touched chrome://extensions.
if ($ExtensionId) {
    if ($ExtensionId -notmatch '^[a-p]{32}$') {
        Fail "The supplied extension ID is not 32 characters of a-p: $ExtensionId" 'Copy it from chrome://extensions, or omit -ExtensionId and let it be resolved.'
    }
    $idSource = 'given on the command line'
} else {
    $resolverPath = Join-Path $PSScriptRoot 'extension_id.py'
    $resolved = (& $python.Source $resolverPath $extensionPath | Out-String) | ConvertFrom-Json
    if (-not $resolved.id) {
        Fail 'Could not work out the extension ID.' 'Load the extension in chrome://extensions and pass its ID with -ExtensionId.'
    }
    $ExtensionId = $resolved.id
    $idSource = switch ($resolved.source) {
        'chrome'  { 'read from Chrome, which has this directory loaded' }
        'derived' { 'derived from the extension path; Chrome has not loaded it yet' }
        default   { $resolved.source }
    }
}
Write-Output "  Extension ID $ExtensionId ($idSource)"

$go = Get-Command go -ErrorAction SilentlyContinue
if (-not $go) {
    Fail 'The Go toolchain was not found on PATH.' 'Install Go 1.24 or newer. It builds the small launcher that starts the Python companion; it is not a runtime dependency.'
}
Write-Output "  $((& go version))"

$chromeKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
if (Test-Path -LiteralPath $chromeKey) {
    $chromeExe = (Get-Item -LiteralPath $chromeKey).GetValue('')
    if ($chromeExe -and (Test-Path -LiteralPath $chromeExe -PathType Leaf)) {
        $chromeVersion = (Get-Item -LiteralPath $chromeExe).VersionInfo.ProductVersion
        if ([int]($chromeVersion.Split('.')[0]) -lt 125) {
            Fail "Google Chrome $chromeVersion is older than the required 125." 'Update Chrome. The extension manifest sets minimum_chrome_version 125.'
        }
        Write-Output "  Google Chrome $chromeVersion"
    }
} else {
    Write-Warning 'Google Chrome was not found in the registry. Confirm Chrome 125 or newer is installed before using the extension.'
}

if ($WhatIf) {
    Write-Output ''
    Write-Output "Would build:    $hostPath"
    Write-Output "Would write:    $manifestPath"
    Write-Output "Would register: $registryPath"
    Write-Output "Would pin origin: chrome-extension://$ExtensionId/"
    exit 0
}

# ---------- install ----------

& (Join-Path $PSScriptRoot 'build.ps1')
if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
    Fail "The native host was not built: $hostPath" 'Read the Go build output above.'
}

$template = Get-Content -LiteralPath $templatePath -Raw -Encoding UTF8
$rendered = $template.Replace('__HOST_PATH__', $hostPath.Replace('\', '\\')).Replace('__EXTENSION_ID__', $ExtensionId)
Set-Content -LiteralPath $manifestPath -Value $rendered -Encoding UTF8 -NoNewline
New-Item -Path $registryPath -Force | Out-Null
Set-Item -LiteralPath $registryPath -Value $manifestPath

# ---------- postflight ----------

Write-Output 'Verifying the installation.'

$written = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$expectedOrigin = "chrome-extension://$ExtensionId/"
if ($written.name -ne 'com.sina.externalcall') {
    Fail "The rendered manifest names the host '$($written.name)'." 'The template was modified; restore it.'
}
if (-not (Test-Path -LiteralPath $written.path -PathType Leaf)) {
    Fail "The rendered manifest points at a host binary that does not exist: $($written.path)" 'Rerun this script.'
}
if ($written.allowed_origins.Count -ne 1 -or $written.allowed_origins[0] -ne $expectedOrigin) {
    Fail "The rendered manifest pins '$($written.allowed_origins -join ', ')' rather than $expectedOrigin." 'Rerun this script, passing -ExtensionId with the ID shown in chrome://extensions.'
}
$registered = (Get-Item -LiteralPath $registryPath).GetValue('')
if ($registered -ne $manifestPath) {
    Fail "The registry key points at '$registered' rather than $manifestPath." 'Rerun this script from this checkout.'
}

Write-Output "  Host manifest: $manifestPath"
Write-Output "  Pinned origin: $expectedOrigin"
Write-Output "  Registered:    $registryPath"

# The companion collects finished downloads from one directory. It defaults to
# the user profile's Downloads folder, but Chrome lets the user choose another,
# and when the two disagree a call validates as if nothing was ever returned.
# Chrome records the choice in its own preferences, so the mismatch can be
# reported here instead of being discovered at the end of a real call.
$expectedDownloads = Join-Path $env:USERPROFILE 'Downloads'
$preferences = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data\Default\Preferences'
if (Test-Path -LiteralPath $preferences -PathType Leaf) {
    try {
        $chosen = (Get-Content -LiteralPath $preferences -Raw -Encoding UTF8 |
            ConvertFrom-Json).download.default_directory
    } catch {
        $chosen = $null
    }
    if ($chosen -and $chosen.TrimEnd('\') -ne $expectedDownloads.TrimEnd('\')) {
        Write-Warning "Chrome saves downloads to $chosen, but the companion reads $expectedDownloads."
        Write-Output  "  Set EXTERNALCALL_DOWNLOADS_DIR to Chrome's directory, for this account:"
        Write-Output  "    [Environment]::SetEnvironmentVariable('EXTERNALCALL_DOWNLOADS_DIR', '$chosen', 'User')"
        Write-Output  "  Then restart Chrome so the companion inherits it."
    } else {
        Write-Output "  Downloads:     $expectedDownloads"
    }
}
Write-Output ''
Write-Output 'Installed. In chrome://extensions:'
Write-Output '  - if the extension is not loaded yet: enable Developer mode, Load unpacked,'
Write-Output "    and pick $extensionPath"
Write-Output '  - if it is already loaded: reload it'
Write-Output 'Then open its side panel: a green dot means the companion answered.'
Write-Output ''
Write-Output "Chrome should show the ID $ExtensionId. If it shows a different one, rerun"
Write-Output 'this script with -ExtensionId and that value.'
Write-Output 'If the panel says the companion is unavailable, reload the extension first.'
