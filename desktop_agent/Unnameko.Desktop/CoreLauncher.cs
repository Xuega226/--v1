using System.Diagnostics;
using System.IO;

namespace Unnameko.Desktop;

internal static class CoreLauncher
{
    public static string? FindProjectRoot()
    {
        var configured = Environment.GetEnvironmentVariable("UNNAMEKO_ROOT");
        if (!string.IsNullOrWhiteSpace(configured) &&
            File.Exists(Path.Combine(configured, "desktop_agent_core.py")))
        {
            return Path.GetFullPath(configured);
        }

        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "desktop_agent_core.py")))
            {
                return current.FullName;
            }
            current = current.Parent;
        }
        return null;
    }

    public static bool TryStart()
    {
        var root = FindProjectRoot();
        if (root is null)
        {
            return false;
        }

        var candidates = new[]
        {
            Path.Combine(root, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(root, ".venv", "Scripts", "python.exe"),
        };
        var python = candidates.FirstOrDefault(File.Exists) ?? "pythonw.exe";
        var info = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "desktop_agent_core.py",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        Process.Start(info);
        return true;
    }
}
