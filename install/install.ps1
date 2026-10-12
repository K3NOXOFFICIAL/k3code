<#
k3code installer for Windows. k3code runs in WSL (Windows Subsystem for Linux): this script runs
install/install.sh inside a WSL distribution and adds k3code.exe and k3.exe to Windows, so `k3code`
works from PowerShell and cmd and opens in the current directory. The two are a small launcher (install/k3launch.cs)
built here with the .NET Framework compiler every Windows has; without it, k3code.cmd and k3.cmd are written instead
(cmd.exe re-reads their arguments: & | < > ^ inside quotes break them). When WSL has no Linux distribution yet,
it runs `wsl --install -d Ubuntu` first (Windows asks for administrator approval once). Everything else
k3code needs inside WSL (uv, Python, Node, Go) is fetched by install.sh without root.

  powershell -ExecutionPolicy Bypass -File install\install.ps1                 # the latest v* tag, else Main
  powershell -ExecutionPolicy Bypass -File install\install.ps1 --from-source   # this checkout

Every argument this script does not know is passed to install.sh unchanged (see install.sh --help).
Own options:
  -Distro NAME     the WSL distribution to install into (default: the default distribution)
  -NoModifyPath    do not add the k3code.cmd directory to your user PATH
  -Help            this text (pass --help for the install.sh options)

Windows shims go to %LOCALAPPDATA%\k3code\bin. Uninstall with install\uninstall.ps1.
Env (for tests): K3_WSL (the wsl executable to call; skips the Windows check).
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Distro = '',
  [switch]$NoModifyPath,
  [switch]$Help,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$InstallArgs = @()
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RawUrl = 'https://raw.githubusercontent.com/K3NOXOFFICIAL/k3code/Main/install/install.sh'
$Wsl = if ($env:K3_WSL) { $env:K3_WSL } else { 'wsl.exe' }
$env:WSL_UTF8 = '1' # wsl.exe prints UTF-16 unless asked; newer WSL honours this, older output is cleaned below

function Say([string]$Text) { [Console]::Error.WriteLine("k3code-install: $Text") }
function Die([string]$Text) { Say "ERROR: $Text"; exit 1 }

function Quote-Sh([string]$Arg) { "'" + ($Arg -replace "'", "'\''") + "'" } # one sh word, no double quotes

# Windows PowerShell 5.1 (and pwsh before 7.3, or with $PSNativeCommandArgumentPassing = 'Legacy') hands a native
# program its arguments without escaping embedded double quotes: the `cd "$(wslpath -a "$1")" && ...` command reached
# sh cut apart ("Syntax error: end of file unexpected"). There the quotes are escaped by hand, as the Windows
# command-line parser reads them back (backslashes before a quote doubled, then \"); newer pwsh escapes them itself.
function ConvertTo-NativeArgs([string[]]$NativeArgs) {
  $mode = Get-Variable -Name PSNativeCommandArgumentPassing -ValueOnly -ErrorAction SilentlyContinue
  if ($mode -and "$mode" -ne 'Legacy') { return $NativeArgs }
  return @($NativeArgs | ForEach-Object { $_ -replace '(\\*)"', '$1$1\"' })
}

function Get-WslText([string[]]$WslArgs) { # runs wsl.exe and returns its stdout as clean text
  # Windows PowerShell 5.1 turns a native command's stderr into an error record, which 'Stop' makes terminating
  $ErrorActionPreference = 'Continue'
  # Linux programs print UTF-8; Windows PowerShell 5.1 would decode it with the OEM codepage (C:\Users\Jörg breaks)
  $enc = [Console]::OutputEncoding
  try { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $out = & $Wsl @WslArgs 2>$null }
  finally { [Console]::OutputEncoding = $enc }
  if ($LASTEXITCODE -ne 0) { return $null }
  return (($out | Out-String) -replace "`0", '').Trim()
}

function Get-Distros { # the installed distributions: @{ Name; Default; Version }
  $text = Get-WslText @('--list', '--verbose')
  if (-not $text) { return @() }
  $rows = @()
  foreach ($line in ($text -split "`r?`n" | Select-Object -Skip 1)) {
    if ($line -match '^\s*(\*?)\s*(\S+)\s+\S+\s+(\d+)\s*$') {
      $rows += [pscustomobject]@{ Name = $Matches[2]; Default = ($Matches[1] -eq '*'); Version = [int]$Matches[3] }
    }
  }
  return $rows
}

function Get-ShArgs([string]$Command, [string]$Cwd = '') { # wsl.exe arguments that run COMMAND in a login sh
  # (a login shell reads ~/.profile, so PATH has what the user installed). The caller runs `& $Wsl @a` itself:
  # wsl.exe output must stay on the console, not in a PowerShell pipe, or install.sh has no terminal to ask on.
  # The directory is changed inside sh (wsl.exe --cd needs a newer WSL than Windows 10 ships with), from the Windows
  # path given as an argument: wsl.exe passes arguments as UTF-16, so no codepage touches a non-ASCII path.
  if ($Cwd) {
    return ConvertTo-NativeArgs @('-d', $script:Distro, '--exec', 'sh', '-lc', "cd `"`$(wslpath -a `"`$1`")`" && $Command", 'sh', $Cwd)
  }
  return ConvertTo-NativeArgs @('-d', $script:Distro, '--exec', 'sh', '-lc', $Command)
}

# The user PATH is a REG_EXPAND_SZ holding %VARIABLES%. [Environment]::SetEnvironmentVariable would store it expanded
# as REG_SZ, so it is read and written through the registry unexpanded instead.
function Get-UserPath {
  if ($env:OS -ne 'Windows_NT') { return $null }
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
  if (-not $key) { return $null }
  try { return [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
  finally { $key.Close() }
}

function Set-UserPath([string]$Value) {
  if ($env:OS -ne 'Windows_NT') { return } # elsewhere (tests) there is no user PATH to write
  $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
  try { $key.SetValue('Path', $Value, [Microsoft.Win32.RegistryValueKind]::ExpandString) } finally { $key.Close() }
  # a throwaway variable set and removed through .NET broadcasts WM_SETTINGCHANGE, so new terminals see the change
  [Environment]::SetEnvironmentVariable('K3CODE_PATH_REFRESH', '1', 'User')
  [Environment]::SetEnvironmentVariable('K3CODE_PATH_REFRESH', $null, 'User')
}

function Write-CmdShim([string]$Dir, [string]$Name, [string]$Target) {
  # cmd passes %* to wsl.exe, which splits it with the Windows rules; sh then gets each argument as "$@".
  # wsl.exe starts in the Linux path of the current Windows directory by itself (no --cd: Windows 10's WSL lacks it).
  # Only the fallback for a Windows without the .NET Framework compiler (and the tests, off Windows): cmd re-parses
  # %*, so & | < > ^ inside quotes in an argument cut it short and run the rest as a cmd command (issue #142).
  $body = "@echo off`r`n" +
  "wsl.exe -d $($script:Distro) --exec sh -lc `"exec $Target \`"`$@\`"`" $Name %*`r`n"
  Set-Content -Path (Join-Path $Dir "$Name.cmd") -Value $body -NoNewline -Encoding Ascii
}

function Get-Csc { # the .NET Framework 4 C# compiler every Windows 10 and 11 has, or $null
  if ($env:OS -ne 'Windows_NT' -or -not $env:WINDIR) { return $null }
  foreach ($fw in @('Framework64', 'Framework')) {
    $csc = Join-Path $env:WINDIR "Microsoft.NET\$fw\v4.0.30319\csc.exe"
    if (Test-Path $csc) { return $csc }
  }
  return $null
}

function Get-LauncherSource { # install/k3launch.cs: from this checkout, else next to install.sh on GitHub; or $null
  $here = if ($PSScriptRoot) { Join-Path $PSScriptRoot 'k3launch.cs' } else { '' }
  if ($here -and (Test-Path $here)) { return $here }
  $file = Join-Path ([IO.Path]::GetTempPath()) "k3launch-$([guid]::NewGuid().ToString('N')).cs"
  try { Invoke-WebRequest -UseBasicParsing -Uri ($RawUrl -replace 'install\.sh$', 'k3launch.cs') -OutFile $file }
  catch { return $null }
  return $file
}

# k3code.exe / k3.exe: install/k3launch.cs compiled with the Windows .NET Framework compiler and the distribution, the
# Linux command and its name baked in. It hands wsl.exe the arguments exactly as given (no cmd.exe in between) and
# returns the exit code. Returns $false when it could not be built; the caller writes the .cmd shim then.
function Write-Launcher([string]$Dir, [string]$Name, [string]$Target, [string]$Source) {
  $csc = Get-Csc
  if (-not $csc -or -not $Source) { return $false }
  $ErrorActionPreference = 'Continue' # csc prints its errors; a failed build falls back to the .cmd shim
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "k3launch-$([guid]::NewGuid().ToString('N'))"
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $lit = { param([string]$s) '@"' + ($s -replace '"', '""') + '"' } # a C# verbatim string
    $config = "static class Config`r`n{`r`n" +
    "    public const string Distro = $(& $lit $script:Distro);`r`n" +
    "    public const string Target = $(& $lit $Target);`r`n" +
    "    public const string Name = $(& $lit $Name);`r`n}`r`n"
    $configFile = Join-Path $tmp 'config.cs'
    Set-Content -Path $configFile -Value $config -Encoding UTF8
    $exe = Join-Path $tmp "$Name.exe"
    $out = & $csc /nologo /target:exe /platform:anycpu /optimize+ "/out:$exe" $Source $configFile
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
      Say "could not build $Name.exe ($(($out | Out-String).Trim()))"
      return $false
    }
    $dest = Join-Path $Dir "$Name.exe"
    if (Test-Path $dest) { # a running k3code.exe cannot be replaced, but it can be renamed out of the way
      Rename-Item -Path $dest -NewName "$Name.exe.old-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    }
    Move-Item -Path $exe -Destination $dest
    Get-ChildItem -Path $Dir -Filter "$Name.exe.old-*" | Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Dir "$Name.cmd") # the .exe wins anyway; no stale shim
    return $true
  } catch {
    Say "could not build $Name.exe: $($_.Exception.Message)"
    return $false
  } finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmp
  }
}

function Write-Shim([string]$Dir, [string]$Name, [string]$Target, [string]$Source) {
  if (Write-Launcher $Dir $Name $Target $Source) { return }
  Write-CmdShim $Dir $Name $Target
  if ($env:OS -eq 'Windows_NT') {
    Say "WARNING: wrote $Name.cmd instead of $Name.exe: cmd.exe re-reads its arguments, so do not put & | < > ^ inside quotes in a $Name argument"
  }
}

if ($Help) {
  $text = Get-Content -Raw $PSCommandPath
  Write-Host ($text.Substring(2, $text.IndexOf('#>') - 2).Trim())
  Write-Host "`nFor the install.sh options, pass --help (it runs install.sh --help in WSL)."
  exit 0
}

# ---- WSL --------------------------------------------------------------------
if (-not $env:K3_WSL -and $env:OS -ne 'Windows_NT') {
  Die 'install.ps1 is for Windows. On Linux and macOS run: sh install/install.sh'
}
if (-not (Get-Command $Wsl -ErrorAction SilentlyContinue)) {
  Die 'this Windows has no wsl.exe: k3code needs Windows 10 version 2004 or later, or Windows 11, for WSL'
}
# Docker Desktop's own distributions (docker-desktop, docker-desktop-data) can be the default one, but are not for
# installing into: they are skipped unless -Distro names one.
function Get-UsableDistros { return @(Get-Distros | Where-Object { $_.Name -notlike 'docker-desktop*' }) }
$distros = @(Get-UsableDistros)
$named = $Distro -and @(Get-Distros | Where-Object { $_.Name -eq $Distro }).Count -gt 0
if ($distros.Count -eq 0 -and -not $named) {
  if ($InstallArgs -contains '--no-install-deps') {
    Die "WSL has no Linux distribution yet and --no-install-deps is set. Run 'wsl --install -d Ubuntu', then this installer again."
  }
  # wsl --install turns WSL on (Windows asks for administrator approval), installs Ubuntu and opens it once
  # in this window to choose a Linux user name. Some machines need a restart before Ubuntu can start.
  Say 'WSL has no Linux distribution yet: installing Ubuntu (Windows may ask for administrator approval)'
  & $Wsl --install -d Ubuntu
  if ($LASTEXITCODE -ne 0) {
    Die "wsl --install failed (exit $LASTEXITCODE). Run 'wsl --install -d Ubuntu' in an administrator PowerShell, restart, then run this installer again."
  }
  $distros = @(Get-UsableDistros)
  if ($distros.Count -eq 0) {
    Say 'WSL is set up. Restart Windows if it asked you to, open Ubuntu from the Start menu once to choose your Linux user name, then run this installer again.'
    exit 0
  }
}
if ($Distro) {
  $all = @(Get-Distros)
  $picked = @($all | Where-Object { $_.Name -eq $Distro })
  if ($picked.Count -eq 0) { Die "no WSL distribution named '$Distro' (installed: $(($all | ForEach-Object Name) -join ', '))" }
} else {
  $picked = @($distros | Where-Object { $_.Default })
  if ($picked.Count -eq 0) { $picked = @($distros[0]) }
}
$script:Distro = $picked[0].Name
Say "WSL distribution: $($script:Distro) (WSL $($picked[0].Version))"
if ($picked[0].Version -lt 2) {
  Say "WARNING: $($script:Distro) runs on WSL 1; k3code is faster and the sandbox only works on WSL 2 (wsl --set-version $($script:Distro) 2)"
}

# ---- run install.sh in WSL --------------------------------------------------
for ($i = 0; $i -lt $InstallArgs.Count - 1; $i++) { # a Windows path for --from-bundle becomes its /mnt/... path
  if ($InstallArgs[$i] -eq '--from-bundle' -and $InstallArgs[$i + 1] -match '^[A-Za-z]:[\\/]') {
    $p = Get-WslText @('-d', $script:Distro, '--exec', 'wslpath', '-a', $InstallArgs[$i + 1])
    if ($p) { $InstallArgs[$i + 1] = $p }
  }
}
$checkout = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { '' } # empty when piped into iex
$local = $checkout -and (Test-Path (Join-Path $PSScriptRoot 'install.sh')) -and
  (Test-Path (Join-Path $checkout 'core\pyproject.toml'))
$quoted = ($InstallArgs | ForEach-Object { Quote-Sh $_ }) -join ' '
if ($local) {
  $a = Get-ShArgs "exec sh install/install.sh $quoted" $checkout
} else {
  Say "no checkout next to this script: running install.sh from $RawUrl"
  $a = Get-ShArgs "curl -fsSL $(Quote-Sh $RawUrl) | sh -s -- $quoted"
}
& $Wsl @a
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (($InstallArgs | Where-Object { $_ -in @('--check', '--print-version', '--no-activate', '-h', '--help') })) {
  exit 0 # nothing was activated: no shims
}

# ---- Windows shims ----------------------------------------------------------
$prefix = ''
for ($i = 0; $i -lt $InstallArgs.Count - 1; $i++) {
  if ($InstallArgs[$i] -eq '--prefix') { $prefix = $InstallArgs[$i + 1] }
}
if (-not $prefix) {
  $prefix = Get-WslText @('-d', $script:Distro, '--exec', 'sh', '-c', 'printf %s $HOME/.local')
  if (-not $prefix) { Die "could not read your home directory in $($script:Distro)" }
}
$binWsl = "$prefix/bin"

if (-not $env:LOCALAPPDATA) { Die 'LOCALAPPDATA is not set' }
$shimDir = Join-Path $env:LOCALAPPDATA 'k3code\bin'
New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
$launcher = if (Get-Csc) { Get-LauncherSource } else { $null } # off Windows (the tests) the .cmd shims are written
Write-Shim $shimDir 'k3code' "$binWsl/k3code" $launcher
$a = Get-ShArgs "test -e $(Quote-Sh "$binWsl/k3")"
& $Wsl @a
if ($LASTEXITCODE -eq 0) {
  Write-Shim $shimDir 'k3' "$binWsl/k3" $launcher
} else {
  foreach ($n in @('k3.cmd', 'k3.exe')) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $shimDir $n) }
}
if ($launcher -and $PSScriptRoot -and -not $launcher.StartsWith($PSScriptRoot)) { # downloaded: a temp file
  Remove-Item -Force -ErrorAction SilentlyContinue $launcher
}
[pscustomobject]@{ distro = $script:Distro; prefix = $prefix } | ConvertTo-Json |
  Set-Content -Path (Join-Path $env:LOCALAPPDATA 'k3code\wsl.json') -Encoding Ascii
Say "Windows commands in $shimDir run k3code in $($script:Distro)"

$userPath = Get-UserPath
$onPath = $userPath -and (($userPath -split ';') -contains $shimDir)
if (-not $onPath) {
  if ($NoModifyPath) {
    Say "Add $shimDir to your PATH to run k3code from PowerShell and cmd"
  } else {
    $new = if ($userPath) { "$userPath;$shimDir" } else { $shimDir }
    Set-UserPath $new
    Say "added $shimDir to your user PATH (new terminals pick it up)"
  }
  $env:Path = "$env:Path;$shimDir"
}
Say 'Done. Next: run `k3code onboard` (in a new terminal) to set up your provider.'
