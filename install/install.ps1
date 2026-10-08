<#
k3code installer for Windows. k3code runs in WSL (Windows Subsystem for Linux): this script runs
install/install.sh inside a WSL distribution and adds k3code.cmd and k3.cmd to Windows, so `k3code`
works from PowerShell and cmd and opens in the current directory. When WSL has no Linux distribution yet,
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

function Get-WslText([string[]]$WslArgs) { # runs wsl.exe and returns its stdout as clean text
  $out = & $Wsl @WslArgs 2>$null
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
  $a = @('-d', $script:Distro)
  if ($Cwd) { $a += @('--cd', $Cwd) }
  return $a + @('--exec', 'sh', '-lc', $Command)
}

function Write-Shim([string]$Dir, [string]$Name, [string]$Target) {
  # cmd passes %* to wsl.exe, which splits it with the Windows rules; sh then gets each argument as "$@"
  $body = "@echo off`r`n" +
  "wsl.exe -d $($script:Distro) --cd `"%CD%`" --exec sh -lc `"exec $Target \`"`$@\`"`" $Name %*`r`n"
  Set-Content -Path (Join-Path $Dir "$Name.cmd") -Value $body -NoNewline -Encoding Ascii
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
$distros = @(Get-Distros)
if ($distros.Count -eq 0) {
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
  $distros = @(Get-Distros)
  if ($distros.Count -eq 0) {
    Say 'WSL is set up. Restart Windows if it asked you to, open Ubuntu from the Start menu once to choose your Linux user name, then run this installer again.'
    exit 0
  }
}
if ($Distro) {
  $picked = @($distros | Where-Object { $_.Name -eq $Distro })
  if ($picked.Count -eq 0) { Die "no WSL distribution named '$Distro' (installed: $(($distros | ForEach-Object Name) -join ', '))" }
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
Write-Shim $shimDir 'k3code' "$binWsl/k3code"
$a = Get-ShArgs "test -e $(Quote-Sh "$binWsl/k3")"
& $Wsl @a
if ($LASTEXITCODE -eq 0) {
  Write-Shim $shimDir 'k3' "$binWsl/k3"
} else {
  Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $shimDir 'k3.cmd')
}
[pscustomobject]@{ distro = $script:Distro; prefix = $prefix } | ConvertTo-Json |
  Set-Content -Path (Join-Path $env:LOCALAPPDATA 'k3code\wsl.json') -Encoding Ascii
Say "Windows commands in $shimDir run k3code in $($script:Distro)"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$onPath = $userPath -and (($userPath -split ';') -contains $shimDir)
if (-not $onPath) {
  if ($NoModifyPath) {
    Say "Add $shimDir to your PATH to run k3code from PowerShell and cmd"
  } else {
    $new = if ($userPath) { "$userPath;$shimDir" } else { $shimDir }
    [Environment]::SetEnvironmentVariable('Path', $new, 'User')
    Say "added $shimDir to your user PATH (new terminals pick it up)"
  }
  $env:Path = "$env:Path;$shimDir"
}
Say 'Done. Next: run `k3code onboard` (in a new terminal) to set up your provider.'
