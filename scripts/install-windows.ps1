#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Audio,
    [string]$BinDirectory = (Join-Path $env:LOCALAPPDATA 'Tacit\bin')
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') {
    throw 'Run this installer in native Windows PowerShell. Use install-macos.sh or install-arch.sh on those systems.'
}
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$BinDirectory = [IO.Path]::GetFullPath($BinDirectory)
$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $uvCommand) {
    throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
$uvPath = $uvCommand.Source
foreach ($name in @('tacit-intent', 'tacit-mcp')) {
    if (Get-Command $name -ErrorAction SilentlyContinue) {
        throw "$name already exists on PATH or in this session. Inspect it before installing this checkout."
    }
    foreach ($suffix in @('', '.ps1', '.exe', '.cmd', '.bat', '.com')) {
        if (Get-Item -LiteralPath (Join-Path $BinDirectory ($name + $suffix)) -Force -ErrorAction SilentlyContinue) {
            throw "$name already exists in $BinDirectory. Inspect it before installing this checkout."
        }
    }
}
$syncArgs = @('sync', '--project', $projectRoot, '--frozen', '--package', 'tacit-cli', '--package', 'tacit-mcp', '--no-default-groups')
if ($Audio) { $syncArgs += @('--extra', 'audio') }
& $uvPath @syncArgs
if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE); no launchers were installed." }
[IO.Directory]::CreateDirectory($BinDirectory) | Out-Null
# PowerShell 5.1 does not preserve embedded quotes/empty arguments when splatting
# into native processes. Build the Windows command line directly; never invoke cmd.
$launcherTemplate = @'
#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
function ConvertTo-TacitNativeArgument([string]$Value) {
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
        } else {
            [void]$builder.Append(('\' * $slashes))
        }
        [void]$builder.Append($character)
        $slashes = 0
    }
    [void]$builder.Append(('\' * (2 * $slashes)))
    [void]$builder.Append('"')
    return $builder.ToString()
}
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = __UV__
$start.UseShellExecute = $false
$forward = @('run', '--project', __PROJECT__, '--package', __PACKAGE__, '--no-sync', __ENTRYPOINT__) + @($args)
$start.Arguments = (($forward | ForEach-Object { ConvertTo-TacitNativeArgument ([string]$_) }) -join ' ')
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
try {
    [void]$process.Start()
    $process.WaitForExit()
    $global:LASTEXITCODE = $process.ExitCode
} finally {
    $process.Dispose()
}
exit $global:LASTEXITCODE
'@
function ConvertTo-TacitLiteral([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
$created = New-Object 'Collections.Generic.List[string]'
$complete = $false
try {
    foreach ($name in @('tacit-intent', 'tacit-mcp')) {
        $package = 'tacit-cli'
        $entrypoint = 'tacit'
        if ($name -eq 'tacit-mcp') { $package = 'tacit-mcp'; $entrypoint = 'tacit-mcp' }
        # Replace in one pass: even token-like filenames remain literal data.
        $literals = @{
            '__UV__' = (ConvertTo-TacitLiteral $uvPath)
            '__PROJECT__' = (ConvertTo-TacitLiteral $projectRoot)
            '__PACKAGE__' = (ConvertTo-TacitLiteral $package)
            '__ENTRYPOINT__' = (ConvertTo-TacitLiteral $entrypoint)
        }
        $content = [regex]::Replace($launcherTemplate, '__(UV|PROJECT|PACKAGE|ENTRYPOINT)__', [Text.RegularExpressions.MatchEvaluator]{ param($match) return $literals[$match.Value] })
        $path = Join-Path $BinDirectory ($name + '.ps1')
        $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $created.Add($path)
        try {
            # BOM preserves Unicode checkout paths in Windows PowerShell 5.1.
            $writer = New-Object IO.StreamWriter($stream, (New-Object Text.UTF8Encoding($true)))
            try { $writer.Write($content) } finally { $writer.Dispose() }
        } finally { $stream.Dispose() }
    }
    $complete = $true
} finally {
    if (-not $complete) {
        foreach ($path in $created) { [IO.File]::Delete($path) }
    }
}
Write-Host "Installed PowerShell client launchers in $BinDirectory. Add that directory to your user PATH if desired."
Write-Host 'This installer does not modify PATH or execution policy. Follow your local PowerShell script policy.'
Write-Host 'Keep this checkout and uv at their current paths; these launchers refer to them.'
Write-Host 'Use -Audio for microphone/playback; the Windows sounddevice wheel supplies PortAudio.'
Write-Host 'Enable microphone access and desktop-app microphone access in Windows Privacy & security settings.'
