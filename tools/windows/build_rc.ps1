#Requires -Version 5.1
<#
.SYNOPSIS
Build the unsigned RC offline from an operator-reviewed clean checkout.
.DESCRIPTION
No downloads or installation. The supplied private Python must exactly match
all files pinned from the approved archive before its executable is invoked.
Missing analysis/toolchain/cache is BLOCKED. Native app execution is separate.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$PythonArchive,
    [Parameter(Mandatory = $true)][string]$WebsocketsWheel,
    [Parameter(Mandatory = $true)][string]$ElectronCache,
    [Parameter(Mandatory = $true)][string]$OutputRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Blocked {
    param([string]$Code, [string]$RequiredCommand)
    @{ schema = 'cafe.windows.rc-build.v1'; status = 'blocked'; code = $Code; required_command = $RequiredCommand } | ConvertTo-Json -Compress
    exit 2
}

function Test-PrivateRuntime {
    param([string]$Executable, [string]$Archive, [string]$PinsFile)
    $pins = Get-Content -LiteralPath $PinsFile -Raw | ConvertFrom-Json
    $archiveInfo = Get-Item -LiteralPath $Archive -Force
    if ($archiveInfo.Length -ne 21987516 -or
        (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06') {
        throw 'APPROVED_ARCHIVE_REQUIRED'
    }
    if ($pins.archive_sha256 -ne '7aba3985d4cee1c7cacb963ca6aa08a5d3e695d7f9609d9fabc56f22ddcf5a06' -or $pins.version -ne '3.13.16+20261013') {
        throw 'PIN_SET_REJECTED'
    }
    $exe = Get-Item -LiteralPath $Executable -Force
    if ($exe.Name -ne 'python.exe' -or $exe.PSIsContainer) { throw 'PYTHON_EXECUTABLE_REQUIRED' }
    $runtime = $exe.Directory.FullName
    $ancestor = $exe.Directory
    while ($null -ne $ancestor) {
        if (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'REPARSE_PATH_REJECTED' }
        $ancestor = $ancestor.Parent
    }
    $expected = @{}
    foreach ($entry in $pins.files.PSObject.Properties) {
        if (-not $entry.Name.StartsWith('python/', [StringComparison]::Ordinal) -or
            $entry.Name.Contains('..') -or $entry.Name.Contains('\') -or $entry.Name.Contains(':')) { throw 'PIN_PATH_REJECTED' }
        $name = $entry.Name.Substring(7)
        if ($expected.ContainsKey($name)) { throw 'DUPLICATE_PIN' }
        $expected[$name] = $entry.Value
    }
    if ($expected.Count -eq 0 -or $expected.Count -gt 10000) { throw 'PIN_COUNT_REJECTED' }
    $pending = New-Object -TypeName 'System.Collections.Generic.Stack[string]'
    $pending.Push($runtime)
    $files = 0
    $directories = 0
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $directories++
        if ($directories -gt 10000) { throw 'DIRECTORY_LIMIT' }
        foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $item = Get-Item -LiteralPath $entryPath -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'REPARSE_PATH_REJECTED' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName); continue }
            $files++
            $name = $item.FullName.Substring($runtime.Length + 1).Replace('\', '/')
            if ($files -gt 10000 -or -not $expected.ContainsKey($name)) { throw 'UNEXPECTED_RUNTIME_FILE' }
            $pin = $expected[$name]
            if ($item.Length -ne $pin.bytes -or (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.sha256) {
                throw 'PRIVATE_RUNTIME_CHANGED'
            }
        }
    }
    if ($files -ne $expected.Count) { throw 'PRIVATE_RUNTIME_INCOMPLETE' }
}

try {
    if ($env:OS -ne 'Windows_NT') { Write-Blocked 'WINDOWS_REQUIRED' 'Run tools/windows/build_rc.ps1 on Windows x64' }
    $analysis = Get-Module -ListAvailable -Name PSScriptAnalyzer
    if ($null -eq $analysis) { Write-Blocked 'PSSCRIPTANALYZER_UNAVAILABLE' 'Get-Module -ListAvailable -Name PSScriptAnalyzer' }
    Import-Module -Name PSScriptAnalyzer -ErrorAction Stop
    $findings = @(Invoke-ScriptAnalyzer -Path $PSCommandPath -Severity Error, Warning)
    if ($findings.Count -ne 0) { Write-Blocked 'SCRIPT_ANALYSIS_FAILED' 'Invoke-ScriptAnalyzer -Path tools/windows/build_rc.ps1 -Severity Error,Warning' }
    $repo = [IO.Path]::GetFullPath((Join-Path -Path $PSScriptRoot -ChildPath '../..'))
    Test-PrivateRuntime -Executable $PythonExe -Archive $PythonArchive -PinsFile (Join-Path -Path $PSScriptRoot -ChildPath 'python_runtime_pins.json')
    & $PythonExe -I -B (Join-Path -Path $PSScriptRoot -ChildPath 'build_rc_contract.py') --source $repo --commit $ReviewedCommit --python $PythonExe --python-archive $PythonArchive --websockets-wheel $WebsocketsWheel --electron-cache $ElectronCache --output $OutputRoot
    exit $LASTEXITCODE
}
catch {
    Write-Blocked 'PREFLIGHT_REJECTED' 'Run the documented checks for the approved private Python and local cache'
}
