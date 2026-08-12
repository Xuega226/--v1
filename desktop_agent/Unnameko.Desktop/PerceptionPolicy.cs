using System.Text.RegularExpressions;

namespace Unnameko.Desktop;

internal enum PerceptionMode
{
    Privacy,
    Companion,
    Agent,
}

internal sealed class PerceptionPolicy
{
    private static readonly Regex WindowContextPattern = new(
        @"看看|看一下|看下|瞅瞅|屏幕|窗口|界面|页面|按钮|这里|这个|当前|显示什么|报错|怎么点|怎么操作",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);

    private static readonly Regex ScreenshotPattern = new(
        @"看看|看一下|看下|瞅瞅|截图|画面|屏幕|图里|窗口.{0,6}(内容|什么|情况)",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);

    public PerceptionMode Mode { get; set; } = PerceptionMode.Companion;
    public bool Paused { get; set; }
    public bool SendSummariesToModel { get; set; } = true;
    public HashSet<string> TrustedApps { get; private set; } = new(StringComparer.OrdinalIgnoreCase);
    public HashSet<string> BlockedApps { get; private set; } = new(StringComparer.OrdinalIgnoreCase);

    public void SetTrustedApps(string? value) => TrustedApps = ParseApps(value);
    public void SetBlockedApps(string? value) => BlockedApps = ParseApps(value);

    public bool IsTrusted(string? processName) => Contains(TrustedApps, processName);
    public bool IsBlocked(string? processName) => Contains(BlockedApps, processName);
    public bool WantsWindowContext(string? text) => WindowContextPattern.IsMatch(text ?? string.Empty);
    public bool WantsScreenshot(string? text) => ScreenshotPattern.IsMatch(text ?? string.Empty);

    public bool CanReadFromButton(string? processName, bool checkedConsent)
    {
        if (Paused || IsBlocked(processName)) return false;
        return Mode != PerceptionMode.Privacy || checkedConsent;
    }

    public bool CanUseNaturalLanguage(string? processName, string? text)
    {
        if (Paused || Mode == PerceptionMode.Privacy || IsBlocked(processName)) return false;
        return WantsWindowContext(text);
    }

    public string TrustedAppsText => string.Join(", ", TrustedApps.OrderBy(value => value));
    public string BlockedAppsText => string.Join(", ", BlockedApps.OrderBy(value => value));

    private static HashSet<string> ParseApps(string? value)
    {
        return new HashSet<string>((value ?? string.Empty)
            .Split([',', '，', ';', '；', '\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
            .Select(Normalize)
            .Where(item => item.Length > 0), StringComparer.OrdinalIgnoreCase);
    }

    private static bool Contains(HashSet<string> apps, string? processName)
    {
        var normalized = Normalize(processName);
        return normalized.Length > 0 && apps.Contains(normalized);
    }

    private static string Normalize(string? value)
    {
        var clean = (value ?? string.Empty).Trim();
        return clean.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? clean[..^4] : clean;
    }
}
