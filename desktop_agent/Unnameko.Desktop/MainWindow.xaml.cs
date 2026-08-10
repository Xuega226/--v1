using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using Forms = System.Windows.Forms;

namespace Unnameko.Desktop;

public partial class MainWindow : Window
{
    private readonly PipeClient _pipe = new();
    private readonly Forms.NotifyIcon _tray;
    private readonly StringBuilder _reply = new();
    private bool _connected;
    private bool _busy;
    private bool _exitRequested;
    private string? _currentApprovalId;

    public MainWindow()
    {
        InitializeComponent();
        _pipe.ConnectionChanged += connected => Dispatcher.Invoke(() => SetConnected(connected));
        _pipe.MessageReceived += message => Dispatcher.Invoke(() => HandleMessage(message));

        _tray = new Forms.NotifyIcon
        {
            Text = "未名子桌面 Agent",
            Icon = System.Drawing.SystemIcons.Information,
            Visible = true,
        };
        var showItem = new Forms.ToolStripMenuItem("显示未名子");
        showItem.Click += (_, _) => Dispatcher.Invoke(ShowFromTray);
        var exitItem = new Forms.ToolStripMenuItem("退出窗口（核心继续运行）");
        exitItem.Click += (_, _) => Dispatcher.Invoke(ExitWindow);
        _tray.ContextMenuStrip = new Forms.ContextMenuStrip();
        _tray.ContextMenuStrip.Items.Add(showItem);
        _tray.ContextMenuStrip.Items.Add(new Forms.ToolStripSeparator());
        _tray.ContextMenuStrip.Items.Add(exitItem);
        _tray.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);

        Loaded += (_, _) =>
        {
            PositionNearDesktopEdge();
            _ = _pipe.RunAsync();
            InputBox.Focus();
        };
        Closing += (_, e) =>
        {
            if (!_exitRequested)
            {
                e.Cancel = true;
                Hide();
            }
        };
    }

    private void PositionNearDesktopEdge()
    {
        var area = SystemParameters.WorkArea;
        Left = area.Right - Width - 22;
        Top = area.Bottom - Height - 22;
    }

    private void SetConnected(bool connected)
    {
        _connected = connected;
        ConnectionDot.Fill = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(
            connected ? "#78A98C" : "#D5A6A6"));
        if (!connected)
        {
            _busy = false;
            InputBox.IsEnabled = true;
            ActivityText.Text = "正在寻找桌面核心…";
            MoodText.Text = "状态：重连中";
        }
        else
        {
            _ = RefreshTaskStateAsync();
        }
    }

    private void HandleMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var typeElement)) return;
        var type = typeElement.GetString();
        switch (type)
        {
            case "status":
                if (message.TryGetProperty("status", out var status))
                {
                    ActivityText.Text = status.TryGetProperty("activity", out var activity)
                        ? activity.GetString() ?? "安静待在电脑里"
                        : "安静待在电脑里";
                    MoodText.Text = "心情：" + (status.TryGetProperty("mood", out var mood)
                        ? mood.GetString() ?? "平静" : "平静");
                }
                break;
            case "chat.started":
                _busy = true;
                _reply.Clear();
                ActivityText.Text = "正在认真听主人说话…";
                AppendChat("\n\n未名子\n", true);
                break;
            case "chat.token":
                if (message.TryGetProperty("content", out var content))
                {
                    var token = content.GetString() ?? string.Empty;
                    _reply.Append(token);
                    AppendChat(token, false);
                }
                break;
            case "chat.tool":
                ActivityText.Text = "正在帮主人查找资料…";
                break;
            case "chat.done":
                _busy = false;
                InputBox.IsEnabled = true;
                InputBox.Focus();
                break;
            case "chat.error":
                _busy = false;
                InputBox.IsEnabled = true;
                var error = message.TryGetProperty("message", out var errorElement)
                    ? errorElement.GetString() : "回复时出了点问题";
                AppendChat($"\n（{error}）", false);
                InputBox.Focus();
                break;
            case "tasks.snapshot":
                if (message.TryGetProperty("tasks", out var tasks))
                {
                    RenderTasks(tasks);
                }
                break;
            case "approvals.snapshot":
                if (message.TryGetProperty("approvals", out var approvals) &&
                    approvals.ValueKind == JsonValueKind.Array && approvals.GetArrayLength() > 0)
                {
                    ShowApproval(approvals[0]);
                }
                else
                {
                    HideApproval();
                }
                break;
            case "approval.pending":
                if (message.TryGetProperty("approval", out var pendingApproval))
                {
                    ShowApproval(pendingApproval);
                }
                break;
            case "task.created":
                TaskFeedbackText.Text = "任务已经保存，等待主人确认后才会执行。";
                if (message.TryGetProperty("approval", out var newApproval))
                {
                    ShowApproval(newApproval);
                }
                _ = RefreshTaskStateAsync();
                break;
            case "task.updated":
                _ = RefreshTaskStateAsync();
                break;
            case "approval.decided":
                HideApproval();
                TaskFeedbackText.Text = "权限决定已经记录。";
                _ = RefreshTaskStateAsync();
                break;
            case "task.error":
                TaskFeedbackText.Text = message.TryGetProperty("message", out var taskError)
                    ? taskError.GetString() ?? "任务处理失败"
                    : "任务处理失败";
                break;
        }
        ChatScroller.ScrollToEnd();
    }

    private async Task RefreshTaskStateAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new { type = "tasks.list", request_id = Guid.NewGuid().ToString("N") });
            await _pipe.SendAsync(new { type = "approvals.list", request_id = Guid.NewGuid().ToString("N") });
        }
        catch
        {
            SetConnected(false);
        }
    }

    private void RenderTasks(JsonElement tasks)
    {
        if (tasks.ValueKind != JsonValueKind.Array || tasks.GetArrayLength() == 0)
        {
            TaskListText.Text = "还没有任务。";
            return;
        }
        var lines = new List<string>();
        foreach (var task in tasks.EnumerateArray().Take(8))
        {
            var title = task.TryGetProperty("title", out var titleElement)
                ? titleElement.GetString() ?? "未命名任务" : "未命名任务";
            var status = task.TryGetProperty("status", out var statusElement)
                ? StatusLabel(statusElement.GetString()) : "未知";
            var detail = string.Empty;
            if (task.TryGetProperty("steps", out var steps) &&
                steps.ValueKind == JsonValueKind.Array && steps.GetArrayLength() > 0 &&
                steps[0].TryGetProperty("input", out var input) &&
                input.TryGetProperty("relative_path", out var path))
            {
                detail = $"\n   {path.GetString()}";
            }
            lines.Add($"• {title}　[{status}]{detail}");
        }
        TaskListText.Text = string.Join("\n\n", lines);
    }

    private void ShowApproval(JsonElement approval)
    {
        if (!approval.TryGetProperty("approval_id", out var idElement)) return;
        _currentApprovalId = idElement.GetString();
        ApprovalSummaryText.Text = approval.TryGetProperty("summary", out var summary)
            ? summary.GetString() ?? "请求执行一个任务步骤" : "请求执行一个任务步骤";
        var scope = approval.TryGetProperty("scope", out var scopeElement)
            ? scopeElement.GetString() ?? "未名子专属工作区" : "未名子专属工作区";
        ApprovalScopeText.Text = $"作用范围：{scope}\n许可仅对这一个步骤有效，不会成为永久授权。";
        ApprovalCard.Visibility = Visibility.Visible;
    }

    private void HideApproval()
    {
        _currentApprovalId = null;
        ApprovalCard.Visibility = Visibility.Collapsed;
    }

    private static string StatusLabel(string? status) => status switch
    {
        "waiting_approval" => "等待确认",
        "running" => "执行中",
        "completed" => "已完成",
        "failed" => "失败",
        "cancelled" => "已取消",
        _ => status ?? "未知",
    };

    private async Task SendCurrentAsync()
    {
        var text = InputBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(text) || _busy) return;
        if (!_connected)
        {
            AppendChat("\n\n系统\n桌面核心还在连接，请稍等一下。", true);
            return;
        }
        InputBox.Clear();
        InputBox.IsEnabled = false;
        AppendChat($"\n\n主人\n{text}", true);
        try
        {
            await _pipe.SendAsync(new
            {
                type = "chat.send",
                request_id = Guid.NewGuid().ToString("N"),
                text,
            });
        }
        catch (Exception ex)
        {
            _busy = false;
            InputBox.IsEnabled = true;
            SetConnected(false);
            var detail = ex.Message.Contains("broken", StringComparison.OrdinalIgnoreCase)
                ? "连接刚刚断开，正在自动重连；这条消息尚未送达，请稍后重新发送。"
                : ex.Message;
            AppendChat($"\n\n系统\n{detail}", true);
        }
        ChatScroller.ScrollToEnd();
    }

    private void AppendChat(string text, bool heading)
    {
        ChatText.Inlines.Add(new System.Windows.Documents.Run(text)
        {
            FontWeight = heading ? FontWeights.SemiBold : FontWeights.Normal,
            Foreground = heading
                ? new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString("#668E76"))
                : (System.Windows.Media.Brush)FindResource("InkBrush"),
        });
    }

    private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed) DragMove();
    }

    private void HideButton_Click(object sender, RoutedEventArgs e) => Hide();

    private async void SendButton_Click(object sender, RoutedEventArgs e) => await SendCurrentAsync();

    private async void CreateTaskButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected)
        {
            TaskFeedbackText.Text = "桌面核心尚未连接，请稍等。";
            return;
        }
        var title = TaskTitleBox.Text.Trim();
        var path = TaskPathBox.Text.Trim();
        var content = TaskContentBox.Text;
        if (string.IsNullOrWhiteSpace(title) || string.IsNullOrWhiteSpace(path) || string.IsNullOrWhiteSpace(content))
        {
            TaskFeedbackText.Text = "请填写任务名称、文件位置和内容。";
            return;
        }
        TaskFeedbackText.Text = "正在保存任务…";
        try
        {
            await _pipe.SendAsync(new
            {
                type = "task.create",
                request_id = Guid.NewGuid().ToString("N"),
                title,
                relative_path = path,
                content,
            });
        }
        catch (Exception ex)
        {
            TaskFeedbackText.Text = ex.Message;
        }
    }

    private async Task DecideApprovalAsync(bool approve)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentApprovalId)) return;
        var approvalId = _currentApprovalId;
        TaskFeedbackText.Text = approve ? "正在执行已允许的步骤…" : "正在记录拒绝…";
        try
        {
            await _pipe.SendAsync(new
            {
                type = "approval.decide",
                request_id = Guid.NewGuid().ToString("N"),
                approval_id = approvalId,
                approve,
            });
        }
        catch (Exception ex)
        {
            TaskFeedbackText.Text = ex.Message;
        }
    }

    private async void ApproveApprovalButton_Click(object sender, RoutedEventArgs e) =>
        await DecideApprovalAsync(true);

    private async void RejectApprovalButton_Click(object sender, RoutedEventArgs e) =>
        await DecideApprovalAsync(false);

    private async void InputBox_KeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers != ModifierKeys.Shift)
        {
            e.Handled = true;
            await SendCurrentAsync();
        }
    }

    private void ShowFromTray()
    {
        Show();
        WindowState = WindowState.Normal;
        Activate();
        Topmost = true;
        InputBox.Focus();
    }

    private async void ExitWindow()
    {
        _exitRequested = true;
        _tray.Visible = false;
        _tray.Dispose();
        await _pipe.DisposeAsync();
        Close();
        System.Windows.Application.Current.Shutdown();
    }
}
