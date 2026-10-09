<#
Remove k3code from Windows: runs install/uninstall.sh in the WSL distribution install.ps1 used, then
deletes the k3code.cmd/k3.cmd shims and their PATH entry. Your data in WSL (~/.k3code, ~/.config/k3code)
is kept unless you pass -Purge.

  powershell -ExecutionPolicy Bypass -File install\uninstall.ps1 [-Purge] [-WindowsOnly]

  -Purge         also delete your k3code data in WSL
  -WindowsOnly   only remove the Windows shims and PATH entry; leave WSL untouched
Env (for tests): K3_WSL (the wsl executable to call).
#>
[CmdletBinding(PositionalBinding = $false)]
param([switch]$Purge, [switch]$WindowsOnly, [switch]$Help)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RawUrl = 'https://raw.githubusercontent.com/K3NOXOFFICIAL/k3code/Main/install/uninstall.sh'
$Wsl = if ($env:K3_WSL) { $env:K3_WSL } else { 'wsl.exe' }

function Say([string]$Text) { [Console]::Error.WriteLine("k3code-uninstall: $Text") }
function Die([string]$Text) { Say "ERROR: $Text"; exit 1 }
function Quote-Sh([string]$Arg) { "'" + ($Arg -replace "'", "'\''") + "'" }

# The user PATH is a REG_EXPAND_SZ holding %VARIABLES%: read and write it unexpanded (as install.ps1 does), since
# [Environment]::SetEnvironmentVariable would store it expanded as REG_SZ.
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

if ($Help) {
  $text = Get-Content -Raw $PSCommandPath
  Write-Host ($text.Substring(2, $text.IndexOf('#>') - 2).Trim())
  exit 0
}
if (-not $env:LOCALAPPDATA) { Die 'LOCALAPPDATA is not set' }
$home3 = Join-Path $env:LOCALAPPDATA 'k3code'
$shimDir = Join-Path $home3 'bin'
$state = Join-Path $home3 'wsl.json'

if (-not $WindowsOnly) {
  if (-not (Test-Path $state)) { Die "no $($state): k3code was not installed with install.ps1 (use -WindowsOnly to clean up shims)" }
  $cfg = Get-Content -Raw $state | ConvertFrom-Json
  $shArgs = @("--prefix $(Quote-Sh $cfg.prefix)")
  if ($Purge) { $shArgs += @('--purge', '--yes') } # -Purge is the confirmation
  $checkout = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { '' }
  if ($checkout -and (Test-Path (Join-Path $PSScriptRoot 'uninstall.sh'))) {
    # cd inside sh (wsl.exe --cd needs a newer WSL than Windows 10 ships with), from the Windows path as an argument
    & $Wsl -d $cfg.distro --exec sh -lc "cd `"`$(wslpath -a `"`$1`")`" && exec sh install/uninstall.sh $($shArgs -join ' ')" sh $checkout
  } else {
    & $Wsl -d $cfg.distro --exec sh -lc "curl -fsSL $(Quote-Sh $RawUrl) | sh -s -- $($shArgs -join ' ')"
  }
  if ($LASTEXITCODE -ne 0) { Die "uninstall.sh failed in $($cfg.distro); the Windows shims are left in place" }
}

foreach ($n in @('k3code.cmd', 'k3.cmd')) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $shimDir $n) }
Remove-Item -Force -ErrorAction SilentlyContinue $state
if ((Test-Path $shimDir) -and -not (Get-ChildItem $shimDir)) { Remove-Item -Force $shimDir }
if ((Test-Path $home3) -and -not (Get-ChildItem $home3)) { Remove-Item -Force $home3 }
$userPath = Get-UserPath
if ($userPath -and (($userPath -split ';') -contains $shimDir)) {
  Set-UserPath (($userPath -split ';' | Where-Object { $_ -ne $shimDir }) -join ';')
  Say "removed $shimDir from your user PATH"
}
Say 'k3code removed from Windows'
