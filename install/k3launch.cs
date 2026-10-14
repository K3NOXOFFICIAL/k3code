// k3code's Windows launcher (k3code.exe, k3.exe): runs the k3code or k3 command in WSL with the arguments exactly as
// they were given, and returns its exit code.
//
// install.ps1 compiles it with the .NET Framework's csc.exe, which every Windows 10 and 11 has, together with a
// generated Config class (Config.Distro, Config.Target, Config.Name). It replaces the k3code.cmd shim: cmd.exe
// re-parses the %* of a batch file with its own quote and metacharacter rules, so `k3code -p 'say "a & b"'` was cut
// at the & and the rest ran as a cmd command (issue #142). No shell runs between this program and wsl.exe.
// C# 5 only: that is what the .NET Framework's csc.exe compiles.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

static class Launcher
{
    static int Main(string[] args)
    {
        // Ctrl+C reaches wsl.exe too (it shares this console): keep waiting for it and return its exit code
        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
        var line = new StringBuilder();
        foreach (string a in new[] { "-d", Config.Distro, "--exec", "sh", "-lc", "exec " + ShQuote(Config.Target) + " \"$@\"", Config.Name })
            Append(line, a);
        foreach (string a in args)
            Append(line, a);
        var start = new ProcessStartInfo(WslPath(), line.ToString());
        start.UseShellExecute = false; // inherit this console: stdin, stdout and stderr go straight to wsl.exe
        try
        {
            using (Process p = Process.Start(start))
            {
                p.WaitForExit();
                return p.ExitCode;
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine(Config.Name + ": cannot start " + start.FileName + ": " + e.Message);
            return 127;
        }
    }

    // wsl.exe by its full path: a bare name is looked up in the current directory first, so a wsl.exe or wsl.bat in
    // the project would run instead. K3_WSL names a stand-in (the installer's tests use the same variable).
    static string WslPath()
    {
        string over = Environment.GetEnvironmentVariable("K3_WSL");
        if (!string.IsNullOrEmpty(over))
            return over;
        string windows = Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows";
        // a 32-bit process sees SysWOW64 as System32, and wsl.exe is only in the real one
        string system = Environment.Is64BitOperatingSystem && !Environment.Is64BitProcess ? "Sysnative" : "System32";
        return Path.Combine(Path.Combine(windows, system), "wsl.exe");
    }

    // One word for sh -c: single quotes, with each ' as '\''.
    static string ShQuote(string s)
    {
        return "'" + s.Replace("'", "'\\''") + "'";
    }

    // Appends one argument as the Windows command-line parser (CommandLineToArgvW, the C runtime) reads it back:
    // quoted when it is empty or holds a space, tab, newline or quote; backslashes doubled only before a quote.
    static void Append(StringBuilder line, string arg)
    {
        if (line.Length > 0)
            line.Append(' ');
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            line.Append(arg);
            return;
        }
        line.Append('"');
        int slashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                slashes++;
                continue;
            }
            line.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
            line.Append(c);
            slashes = 0;
        }
        line.Append('\\', slashes * 2);
        line.Append('"');
    }
}
