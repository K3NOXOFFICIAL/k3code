<#
Windows-only check of the k3code.exe launcher (install/k3launch.cs, built by install.ps1's Write-Launcher): every
argument reaches wsl.exe exactly as given, nothing in an argument runs as a cmd command, the exit code comes back
(issue #142). Needs Windows with the .NET Framework 4 (every Windows 10 and 11); run it from Windows PowerShell or pwsh:

  powershell -ExecutionPolicy Bypass -File scripts\ci\windows-launcher.ps1

It takes Write-Launcher and Get-Csc from install.ps1 itself, builds k3code.exe against a stand-in wsl.exe (K3_WSL)
that prints its arguments, and runs the cases from the issue. Exit status 0 when every case holds.
#>
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { Write-Host 'windows-launcher: skipped (not Windows)'; exit 0 }

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$work = Join-Path ([IO.Path]::GetTempPath()) "k3-launcher-check-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
New-Item -ItemType Directory -Path $work | Out-Null
$failed = 0
function Say([string]$Text) { [Console]::Error.WriteLine("windows-launcher: $Text") }
function Check([bool]$Ok, [string]$What) {
  if ($Ok) { Write-Host "ok    $What" } else { Write-Host "FAIL  $What"; $script:failed++ }
}

# the installer's own functions, not a copy
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'install\install.ps1'), [ref]$null, [ref]$null)
foreach ($name in @('Get-Csc', 'Write-Launcher')) {
  $fn = $ast.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
  . ([scriptblock]::Create($fn.Extent.Text))
}
$csc = Get-Csc
if (-not $csc) { Say 'no .NET Framework csc.exe'; exit 1 }

try {
  # a wsl.exe stand-in: prints every argument as ARG[i]=<...> and exits with K3_FAKE_EXIT
  $fakeSrc = Join-Path $work 'fakewsl.cs'
  Set-Content -Path $fakeSrc -Encoding UTF8 -Value @'
using System;
static class FakeWsl {
    static int Main(string[] args) {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        for (int i = 0; i < args.Length; i++) Console.WriteLine("ARG[" + i + "]=<" + args[i] + ">");
        string code = Environment.GetEnvironmentVariable("K3_FAKE_EXIT");
        return string.IsNullOrEmpty(code) ? 0 : int.Parse(code);
    }
}
'@
  $fake = Join-Path $work 'wsl.exe'
  & $csc /nologo /target:exe "/out:$fake" $fakeSrc | Out-Null
  if ($LASTEXITCODE -ne 0) { Say 'could not build the wsl.exe stand-in'; exit 1 }

  $bin = Join-Path $work 'bin'
  New-Item -ItemType Directory -Path $bin | Out-Null
  $script:Distro = 'Ubuntu'
  $built = Write-Launcher $bin 'k3code' '/home/u/.local/bin/k3code' (Join-Path $repo 'install\k3launch.cs')
  Check $built 'install.ps1 Write-Launcher builds k3code.exe'
  if (-not $built) { exit 1 }
  $exe = Join-Path $bin 'k3code.exe'
  $env:K3_WSL = $fake

  # what the stand-in received after the fixed prefix (-d Ubuntu --exec sh -lc <script> k3code)
  function Get-Received([string[]]$Lines) {
    $lines = @($Lines | Where-Object { $_ -match '^ARG\[\d+\]=<' })
    Check ($lines.Count -ge 7 -and $lines[0] -eq 'ARG[0]=<-d>' -and $lines[1] -eq 'ARG[1]=<Ubuntu>' -and
      $lines[5] -eq "ARG[5]=<exec '/home/u/.local/bin/k3code' `"`$@`">" -and $lines[6] -eq 'ARG[6]=<k3code>') 'the wsl.exe prefix'
    return @($lines | Select-Object -Skip 7 | ForEach-Object { $_ -replace '^ARG\[\d+\]=<(.*)>$', '$1' })
  }

  # 1. exact round trip: the launcher gets a command line quoted as Windows programs expect and must hand wsl.exe the
  #    same words (independent of how this PowerShell version quotes for native programs)
  $cases = @(
    @('-p', 'explain "a && b" in bash'),
    @('-p', 'say "a & echo INJECTED> inj.txt b"'),
    @('-p', 'what does a | b do'),
    @('100% sure', '%USERNAME%', '^caret', 'a<b>c'),
    @('', 'trailing\', 'back\"slash', 'C:\path with space\'),
    @("$([char]0xFC)ber", "na$([char]0xEF)ve `"quoted`" $([char]0x4E16)") # non-ASCII, without depending on this file's encoding
  )
  function Quote-Win([string]$a) { # the same rules as the launcher's Append (CommandLineToArgvW reads them back)
    if ($a.Length -gt 0 -and $a -notmatch '[ \t\n\v"]') { return $a }
    $s = [Text.StringBuilder]::new('"'); $n = 0
    foreach ($c in $a.ToCharArray()) {
      if ($c -eq '\') { $n++; continue }
      [void]$s.Append('\', $(if ($c -eq '"') { 2 * $n + 1 } else { $n })).Append($c); $n = 0
    }
    return $s.Append('\', 2 * $n).Append('"').ToString()
  }
  foreach ($case in $cases) {
    $psi = [Diagnostics.ProcessStartInfo]::new($exe, (($case | ForEach-Object { Quote-Win $_ }) -join ' '))
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.WorkingDirectory = $work
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $p = [Diagnostics.Process]::Start($psi); $out = $p.StandardOutput.ReadToEnd() -split "`r?`n"; $p.WaitForExit()
    $got = Get-Received $out
    Check (($got.Count -eq $case.Count) -and -not (Compare-Object -SyncWindow 0 $got $case -CaseSensitive)) `
      "arguments arrive unchanged: $($case -join ' | ')"
  }

  # 2. the injection from the issue, through PowerShell's own call operator and through cmd.exe: no cmd command runs
  Push-Location $work
  try {
    $null = & $exe -p 'say "a & echo INJECTED> inj.txt b"'
    $null = cmd.exe /d /c "`"$exe`" -p `"say a ^& echo INJECTED2^> inj2.txt b`""
    $null = cmd.exe /d /c "`"$exe`" -p `"x`" ^& echo NOT-PART-OF-K3CODE> inj3.txt"
  } finally { Pop-Location }
  Check (-not (Test-Path (Join-Path $work 'inj.txt'))) 'an & inside a quoted argument (PowerShell) runs nothing'
  Check (-not (Test-Path (Join-Path $work 'inj2.txt'))) 'an escaped & inside a quoted argument (cmd.exe) runs nothing'
  Check (Test-Path (Join-Path $work 'inj3.txt')) 'a real & on the cmd.exe line still separates commands (control)'

  # 3. the exit code comes back
  $env:K3_FAKE_EXIT = '7'
  $null = & $exe --version
  Check ($LASTEXITCODE -eq 7) 'the exit code of wsl.exe is returned'
  $env:K3_FAKE_EXIT = ''
} finally {
  Remove-Item Env:K3_WSL, Env:K3_FAKE_EXIT -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}
if ($failed) { Say "$failed check(s) failed"; exit 1 }
Say 'all checks passed'
