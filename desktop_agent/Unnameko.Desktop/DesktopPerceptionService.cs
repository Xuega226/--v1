using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Automation;

namespace Unnameko.Desktop;

internal sealed record WindowControlInfo(
    int Depth,
    string Kind,
    string Name,
    string AutomationId,
    bool Enabled,
    bool Focusable,
    bool IsPassword,
    int Left,
    int Top,
    int Width,
    int Height);

internal sealed record WindowStructureSnapshot(
    nint Handle,
    string Title,
    string ProcessName,
    IReadOnlyList<WindowControlInfo> Controls,
    string Fingerprint,
    string Warning)
{
    public string ToContext(int maxControls = 50)
    {
        var builder = new StringBuilder();
        builder.Append("【窗口结构】").Append(Title);
        if (!string.IsNullOrWhiteSpace(ProcessName)) builder.Append("（").Append(ProcessName).Append('）');
        builder.AppendLine();

        if (Controls.Count == 0)
        {
            builder.Append("没有读取到可用控件");
            if (!string.IsNullOrWhiteSpace(Warning)) builder.Append("：").Append(Warning);
            return builder.ToString();
        }

        foreach (var control in Controls.Take(Math.Clamp(maxControls, 1, 100)))
        {
            builder.Append(' ', Math.Min(control.Depth, 5) * 2)
                .Append("- ").Append(control.Kind);
            if (control.IsPassword)
            {
                builder.Append("：<密码内容已隐藏>");
            }
            else if (!string.IsNullOrWhiteSpace(control.Name))
            {
                builder.Append("：").Append(control.Name);
            }
            if (!string.IsNullOrWhiteSpace(control.AutomationId))
            {
                builder.Append(" [").Append(control.AutomationId).Append(']');
            }
            builder.Append(" @(").Append(control.Left).Append(',').Append(control.Top)
                .Append(' ').Append(control.Width).Append('×').Append(control.Height).Append(')');
            if (!control.Enabled) builder.Append("（不可用）");
            builder.AppendLine();
        }
        if (Controls.Count > maxControls) builder.Append("…其余 ").Append(Controls.Count - maxControls).Append(" 个控件已省略");
        if (!string.IsNullOrWhiteSpace(Warning)) builder.AppendLine().Append("提示：").Append(Warning);
        return builder.ToString().TrimEnd();
    }
}

internal sealed class DesktopPerceptionService
{
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(nint hWnd, out uint processId);

    [DllImport("user32.dll")]
    internal static extern bool IsWindow(nint hWnd);

    [DllImport("user32.dll")]
    private static extern nint GetDC(nint hWnd);

    [DllImport("user32.dll")]
    private static extern int ReleaseDC(nint hWnd, nint hDc);

    [DllImport("gdi32.dll")]
    private static extern uint GetPixel(nint hdc, int x, int y);

    public WindowStructureSnapshot CaptureStructure(nint handle, int maxControls = 80, int maxDepth = 6)
    {
        if (handle == nint.Zero || !IsWindow(handle)) throw new InvalidOperationException("目标窗口已经关闭");
        var title = ReadAutomationProperty(() => AutomationElement.FromHandle(handle).Current.Name);
        var processName = ReadProcessName(handle);
        var controls = new List<WindowControlInfo>();
        var warning = string.Empty;

        try
        {
            var root = AutomationElement.FromHandle(handle);
            Walk(root, TreeWalker.ControlViewWalker, 0, Math.Clamp(maxDepth, 1, 10),
                Math.Clamp(maxControls, 1, 200), controls);
        }
        catch (Exception ex) when (ex is ElementNotAvailableException or COMException or InvalidOperationException)
        {
            warning = "应用没有开放 UI Automation，后续可使用截图识别兜底";
        }

        var fingerprintText = string.Join('\n', controls.Select(c =>
            $"{c.Depth}|{c.Kind}|{c.Name}|{c.AutomationId}|{c.Enabled}|{c.Left},{c.Top},{c.Width},{c.Height}"));
        var fingerprint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
            $"{title}\n{processName}\n{fingerprintText}")));
        return new WindowStructureSnapshot(handle, title, processName, controls, fingerprint, warning);
    }

    public string GetProcessName(nint handle) => ReadProcessName(handle);

    public byte[] CaptureVisualSample(nint handle, int columns = 24, int rows = 14)
    {
        if (handle == nint.Zero || !IsWindow(handle) || !NativeWindow.GetWindowRect(handle, out var rect))
            throw new InvalidOperationException("目标窗口已经关闭");
        var width = Math.Max(1, rect.Right - rect.Left);
        var height = Math.Max(1, rect.Bottom - rect.Top);
        var result = new byte[Math.Clamp(columns, 4, 64) * Math.Clamp(rows, 4, 64)];
        var screenDc = GetDC(nint.Zero);
        if (screenDc == nint.Zero) throw new InvalidOperationException("无法读取屏幕变化样本");
        try
        {
            var index = 0;
            for (var row = 0; row < rows; row++)
            {
                var y = rect.Top + Math.Clamp((row * height) / rows + height / (rows * 2), 0, height - 1);
                for (var column = 0; column < columns; column++)
                {
                    var x = rect.Left + Math.Clamp((column * width) / columns + width / (columns * 2), 0, width - 1);
                    var color = GetPixel(screenDc, x, y);
                    var red = color & 0xff;
                    var green = (color >> 8) & 0xff;
                    var blue = (color >> 16) & 0xff;
                    result[index++] = (byte)((red * 30 + green * 59 + blue * 11) / 100);
                }
            }
        }
        finally
        {
            ReleaseDC(nint.Zero, screenDc);
        }
        return result;
    }

    public static double Difference(byte[]? previous, byte[] current)
    {
        if (previous is null || previous.Length != current.Length) return 1;
        if (current.Length == 0) return 0;
        long difference = 0;
        for (var i = 0; i < current.Length; i++) difference += Math.Abs(previous[i] - current[i]);
        return difference / (255d * current.Length);
    }

    private static void Walk(
        AutomationElement parent,
        TreeWalker walker,
        int depth,
        int maxDepth,
        int maxControls,
        List<WindowControlInfo> output)
    {
        if (depth > maxDepth || output.Count >= maxControls) return;
        AutomationElement? child;
        try { child = walker.GetFirstChild(parent); }
        catch (ElementNotAvailableException) { return; }

        while (child is not null && output.Count < maxControls)
        {
            try
            {
                var current = child.Current;
                var bounds = current.BoundingRectangle;
                var kind = current.ControlType?.ProgrammaticName?.Replace("ControlType.", "", StringComparison.Ordinal) ?? "Control";
                var isPassword = current.IsPassword;
                output.Add(new WindowControlInfo(
                    depth,
                    Clean(kind, 40),
                    isPassword ? string.Empty : Clean(current.Name, 160),
                    Clean(current.AutomationId, 80),
                    current.IsEnabled,
                    current.IsKeyboardFocusable,
                    isPassword,
                    SafeInt(bounds.Left), SafeInt(bounds.Top), SafeInt(bounds.Width), SafeInt(bounds.Height)));
                Walk(child, walker, depth + 1, maxDepth, maxControls, output);
                child = walker.GetNextSibling(child);
            }
            catch (Exception ex) when (ex is ElementNotAvailableException or COMException or InvalidOperationException)
            {
                try { child = walker.GetNextSibling(child); }
                catch { break; }
            }
        }
    }

    private static string ReadProcessName(nint handle)
    {
        try
        {
            GetWindowThreadProcessId(handle, out var processId);
            return processId == 0 ? string.Empty : Process.GetProcessById((int)processId).ProcessName;
        }
        catch { return string.Empty; }
    }

    private static string ReadAutomationProperty(Func<string> reader)
    {
        try { return Clean(reader(), 300); }
        catch { return string.Empty; }
    }

    private static string Clean(string? value, int maxLength)
    {
        var clean = string.Join(' ', (value ?? string.Empty).Split((char[]?)null,
            StringSplitOptions.RemoveEmptyEntries));
        return clean.Length <= maxLength ? clean : clean[..maxLength] + "…";
    }

    private static int SafeInt(double value) => double.IsNaN(value) || double.IsInfinity(value)
        ? 0 : (int)Math.Clamp(value, int.MinValue, int.MaxValue);
}

internal static class NativeWindow
{
    [DllImport("user32.dll")]
    internal static extern bool GetWindowRect(nint hWnd, out NativeRect rect);
}

[StructLayout(LayoutKind.Sequential)]
internal struct NativeRect
{
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}
