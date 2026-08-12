using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Unnameko.Desktop;

public partial class MainWindow : Window
{
    private readonly PipeClient _pipe = new();
    private readonly Forms.NotifyIcon _tray;
    private readonly Forms.ToolStripMenuItem _pausePerceptionItem;
    private readonly StringBuilder _reply = new();
    private bool _connected;
    private bool _busy;
    private bool _exitRequested;
    private string? _currentApprovalId;
    private string? _currentPlanTaskId;
    private bool _currentPlanIsPresentation;
    private readonly List<string> _presentationPreviewFiles = [];
    private int _presentationPreviewIndex;
    private string _workspacePath = string.Empty;
    private string _pendingPerceptionContext = string.Empty;
    private nint _lastExternalWindow;
    private bool _petMode;
    private double _lastEnergy = 0.5;
    private int _lastActiveTasks;
    private string _lastMood = "平静";
    private readonly StringBuilder _bubbleSentenceBuffer = new();
    private readonly List<BubbleEntry> _bubbleHistory = [];
    private int _bubbleHistoryIndex = -1;
    private bool _bubbleAutoHidePending;
    private BubbleKind _currentBubbleKind = BubbleKind.Speech;
    private string _lastBubbleSegment = string.Empty;
    private readonly DispatcherTimer _windowTracker = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly DispatcherTimer _observationTimer = new() { Interval = TimeSpan.FromSeconds(2) };
    private readonly DispatcherTimer _bubbleTimer = new() { Interval = TimeSpan.FromSeconds(12) };
    private readonly DispatcherTimer _toastTimer = new() { Interval = TimeSpan.FromSeconds(4) };
    private readonly DispatcherTimer _blinkTimer = new() { Interval = TimeSpan.FromSeconds(4.5) };
    private readonly DispatcherTimer _mouthTimer = new() { Interval = TimeSpan.FromMilliseconds(135) };
    private readonly DispatcherTimer _idleGestureTimer = new() { Interval = TimeSpan.FromSeconds(9) };
    private readonly DispatcherTimer _presenceTimer = new() { Interval = TimeSpan.FromSeconds(60) };
    private readonly AvatarExpressionStateMachine _expressionState = new();
    private readonly DesktopPerceptionService _perception = new();
    private readonly PerceptionPolicy _perceptionPolicy = new();
    private readonly Dictionary<string, TaskCompletionSource<string>> _directPerceptionRequests = [];
    private readonly Dictionary<string, ImageSource> _avatarFrames = new(StringComparer.OrdinalIgnoreCase);
    private readonly Random _animationRandom = new();
    private readonly string _presentationStatePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "UnnamekoDesktop", "presentation.json");
    private nint _observationWindow;
    private DateTimeOffset _observationEndsAt;
    private DateTimeOffset _lastObservationContextAt;
    private DateTimeOffset _lastObservationVisionAt;
    private string _lastObservationStructureFingerprint = string.Empty;
    private byte[]? _lastObservationVisualSample;
    private bool _observationTickBusy;
    private bool _observationVisionInFlight;
    private int _observationVisionCount;
    private bool _perceptionPreparing;
    private string _currentExternalProcess = string.Empty;
    private string _currentExternalTitle = string.Empty;
    private int _reactionGeneration;
    private int _emoteGeneration;
    private int _emotePriority;
    private DateTimeOffset _emoteHoldUntil;
    private bool _isWindowDragging;
    private System.Windows.Point _dragStartCursor;
    private System.Windows.Point _lastDragCursor;
    private DateTimeOffset _lastDragSampleAt;
    private double _dragStartLeft;
    private double _dragStartTop;
    private Vector _dragVelocity;
    private DragSnapEdges _dragSnapCandidate;
    private int _snapHintGeneration;
    private string _currentProactiveCandidateId = string.Empty;
    private string _currentProactiveProjectId = string.Empty;
    private string _currentProactiveOpportunityId = string.Empty;
    private string _currentProactiveKind = string.Empty;
    private string _pendingEvidenceProjectId = string.Empty;
    private string _pendingEvidenceOpportunityId = string.Empty;
    private string _pendingSourceProjectId = string.Empty;
    private string _pendingSourceOpportunityId = string.Empty;
    private string _autonomyDraftsDir = string.Empty;
    private bool _autonomyPaused;

    private enum BubbleKind
    {
        Speech,
        Thinking,
        Tool,
        Permission,
        Reminder,
        Proactive,
        Error,
    }

    private enum ReactionCue
    {
        Listening,
        Thinking,
        Speaking,
        Tool,
        Working,
        Success,
        Error,
        Headpat,
        Reminder,
        Permission,
        Welcome,
        Idle,
    }

    [Flags]
    private enum DragSnapEdges
    {
        None = 0,
        Left = 1,
        Right = 2,
        Top = 4,
        Bottom = 8,
    }

    private sealed record BubbleEntry(string Text, BubbleKind Kind, DateTimeOffset CreatedAt);

    private sealed class PresentationState
    {
        public bool PetMode { get; set; }
        public double Left { get; set; }
        public double Top { get; set; }
        public string? PerceptionMode { get; set; }
        public bool PerceptionPaused { get; set; }
        public bool SendPerceptionToModel { get; set; } = true;
        public string? TrustedApps { get; set; }
        public string? BlockedApps { get; set; }
    }

    private sealed class TaskViewItem
    {
        public required string Id { get; init; }
        public required string Status { get; init; }
        public required string Title { get; init; }
        public required string StatusLabel { get; init; }
        public required string ProgressText { get; init; }
        public double ProgressPercent { get; init; }
        public required System.Windows.Media.Brush StatusBrush { get; init; }
        public required string Display { get; init; }
        public string ResultPath { get; init; } = string.Empty;
    }

    private sealed class MemoryViewItem
    {
        public required string Id { get; init; }
        public required string Content { get; init; }
        public bool Pinned { get; init; }
        public string Display => $"{(Pinned ? "📌 " : "")}{Content}\n#{Id[..Math.Min(8, Id.Length)]}";
    }

    private sealed class ReminderViewItem
    {
        public required string Id { get; init; }
        public required string Title { get; init; }
        public required string Message { get; init; }
        public required DateTime DueAt { get; init; }
        public string Display => $"{DueAt:MM-dd HH:mm}　{Title}";
    }

    private sealed class ProactiveViewItem
    {
        public required string LoopId { get; init; }
        public required string Content { get; init; }
        public required string Status { get; init; }
        public required string DueText { get; init; }
        public string Display => $"{Status} · {DueText}\n{Content}";
    }

    private sealed class ProactiveTimelineViewItem
    {
        public required string Display { get; init; }
    }

    private sealed class ProjectArtifactViewItem
    {
        public required string Path { get; init; }
        public required string Role { get; init; }
        public string Display => $"{Role} · {Path}";
    }

    private sealed class ProjectOpportunityViewItem
    {
        public required string Id { get; init; }
        public required string ProjectId { get; init; }
        public required string Title { get; init; }
        public required string Rationale { get; init; }
        public required string Risk { get; init; }
        public required string Goal { get; init; }
        public required string Evidence { get; init; }
        public required string Status { get; init; }
        public double ValueScore { get; init; }
        public string Display => $"{Title} · {Status}\n{Rationale}";
    }

    private sealed class ProjectViewItem
    {
        public required string Id { get; init; }
        public required string Title { get; init; }
        public required string Goal { get; init; }
        public required string Status { get; init; }
        public required string Progress { get; init; }
        public bool Archived { get; init; }
        public List<ProjectArtifactViewItem> Artifacts { get; init; } = [];
        public List<ProjectOpportunityViewItem> Opportunities { get; init; } = [];
        public string Display => $"{Title} · {Status}\n{Progress}";
    }

    private sealed class AutonomyGrantViewItem
    {
        public required string Id { get; init; }
        public required string ProjectId { get; init; }
        public required string Status { get; init; }
        public required string Display { get; init; }
    }

    private sealed class AutonomyJobViewItem
    {
        public required string Id { get; init; }
        public required string Status { get; init; }
        public required string Path { get; init; }
        public required string Detail { get; init; }
        public required string Display { get; init; }
    }

    private sealed class AutonomyAuditViewItem
    {
        public required string Display { get; init; }
    }

    private sealed class AutonomyInboxViewItem
    {
        public required string Id { get; init; }
        public required string Detail { get; init; }
        public required string Display { get; init; }
    }

    private sealed class AutonomyDecisionViewItem
    {
        public required string Display { get; init; }
    }

    private sealed class AutonomyPackageViewItem
    {
        public required string Id { get; init; }
        public required string Status { get; init; }
        public required string Display { get; init; }
    }

    [DllImport("user32.dll")]
    private static extern nint GetForegroundWindow();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(nint hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(nint hWnd, out NativeRect rect);

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect { public int Left; public int Top; public int Right; public int Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct LastInputInfo { public uint Size; public uint Time; }

    [DllImport("user32.dll")]
    private static extern bool GetLastInputInfo(ref LastInputInfo info);

    public MainWindow()
    {
        InitializeComponent();
        _perceptionPolicy.SetTrustedApps("Code, devenv, notepad, explorer, WindowsTerminal, powershell, cmd");
        _perceptionPolicy.SetBlockedApps("1Password, Bitwarden, KeePass, KeePassXC, CredentialUIBroker");
        _pipe.ConnectionChanged += connected => Dispatcher.Invoke(() => SetConnected(connected));
        _pipe.MessageReceived += message => Dispatcher.Invoke(() => HandleMessage(message));
        _windowTracker.Tick += (_, _) => TrackExternalWindow();
        _observationTimer.Tick += async (_, _) => await ObserveWindowTickAsync();
        _bubbleTimer.Tick += (_, _) => HidePetBubble();
        _toastTimer.Tick += (_, _) => HideToast();
        _blinkTimer.Tick += BlinkTimer_Tick;
        _mouthTimer.Tick += (_, _) =>
        {
            _expressionState.AdvanceMouth();
            ApplyAvatarFrame();
        };
        _idleGestureTimer.Tick += (_, _) =>
        {
            if (_connected && !_busy && _lastActiveTasks == 0 && IsVisible)
                PlayReaction(ReactionCue.Idle, _animationRandom.NextDouble() < 0.18);
            ScheduleNextIdleGesture();
        };
        _presenceTimer.Tick += async (_, _) => await SendPresencePulseAsync();
        MouseMove += Window_DragMouseMove;
        MouseLeftButtonUp += Window_DragMouseLeftButtonUp;
        LostMouseCapture += Window_DragLostMouseCapture;

        _tray = new Forms.NotifyIcon
        {
            Text = "未名子桌面 Agent",
            Icon = System.Drawing.SystemIcons.Information,
            Visible = true,
        };
        var showItem = new Forms.ToolStripMenuItem("显示未名子");
        showItem.Click += (_, _) => Dispatcher.Invoke(ShowFromTray);
        var petModeItem = new Forms.ToolStripMenuItem("切换桌宠 / 完整面板");
        petModeItem.Click += (_, _) => Dispatcher.Invoke(() => SetPetMode(!_petMode));
        _pausePerceptionItem = new Forms.ToolStripMenuItem("暂停桌面感知");
        _pausePerceptionItem.Click += (_, _) => Dispatcher.Invoke(() => SetPerceptionPaused(!_perceptionPolicy.Paused));
        var exitItem = new Forms.ToolStripMenuItem("退出窗口（核心继续运行）");
        exitItem.Click += (_, _) => Dispatcher.Invoke(ExitWindow);
        _tray.ContextMenuStrip = new Forms.ContextMenuStrip();
        _tray.ContextMenuStrip.Items.Add(showItem);
        _tray.ContextMenuStrip.Items.Add(petModeItem);
        _tray.ContextMenuStrip.Items.Add(_pausePerceptionItem);
        _tray.ContextMenuStrip.Items.Add(new Forms.ToolStripSeparator());
        _tray.ContextMenuStrip.Items.Add(exitItem);
        _tray.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);

        Loaded += (_, _) =>
        {
            PositionNearDesktopEdge();
            RestorePresentationState();
            RenderPerceptionPolicy();
            LoadAvatarFrames();
            ApplyAvatarFrame();
            ScheduleNextBlink();
            _blinkTimer.Start();
            _ = _pipe.RunAsync();
            _windowTracker.Start();
            StartPresenceAnimations();
            ScheduleNextIdleGesture();
            _idleGestureTimer.Start();
            _presenceTimer.Start();
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

    private void RestorePresentationState()
    {
        try
        {
            if (!File.Exists(_presentationStatePath)) return;
            var state = JsonSerializer.Deserialize<PresentationState>(File.ReadAllText(_presentationStatePath));
            if (state is null) return;
            SetPetMode(state.PetMode, save: false);
            _perceptionPolicy.Mode = state.PerceptionMode?.ToLowerInvariant() switch
            {
                "privacy" => PerceptionMode.Privacy,
                "agent" => PerceptionMode.Agent,
                _ => PerceptionMode.Companion,
            };
            _perceptionPolicy.Paused = state.PerceptionPaused;
            _perceptionPolicy.SendSummariesToModel = state.SendPerceptionToModel;
            if (state.TrustedApps is not null) _perceptionPolicy.SetTrustedApps(state.TrustedApps);
            if (state.BlockedApps is not null) _perceptionPolicy.SetBlockedApps(state.BlockedApps);
            var area = SystemParameters.WorkArea;
            Left = Math.Clamp(state.Left, area.Left, Math.Max(area.Left, area.Right - Width));
            Top = Math.Clamp(state.Top, area.Top, Math.Max(area.Top, area.Bottom - Height));
        }
        catch
        {
            // 表现层状态损坏时使用默认位置，不影响核心功能。
        }
    }

    private void SavePresentationState()
    {
        try
        {
            var directory = Path.GetDirectoryName(_presentationStatePath)!;
            Directory.CreateDirectory(directory);
            File.WriteAllText(_presentationStatePath, JsonSerializer.Serialize(new PresentationState
            {
                PetMode = _petMode,
                Left = Left,
                Top = Top,
                PerceptionMode = _perceptionPolicy.Mode.ToString().ToLowerInvariant(),
                PerceptionPaused = _perceptionPolicy.Paused,
                SendPerceptionToModel = _perceptionPolicy.SendSummariesToModel,
                TrustedApps = _perceptionPolicy.TrustedAppsText,
                BlockedApps = _perceptionPolicy.BlockedAppsText,
            }));
        }
        catch
        {
            // 位置保存失败不应打断桌宠交互。
        }
    }

    private void LoadAvatarFrames()
    {
        string[] names =
        [
            "neutral.png", "happy.png", "shy.png", "worried.png", "sleepy.png",
            "focused.png", "blink.png", "talk_half.png", "talk_open.png",
        ];
        foreach (var name in names)
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri($"pack://application:,,,/Assets/Expressions/{name}", UriKind.Absolute);
            bitmap.EndInit();
            bitmap.Freeze();
            _avatarFrames[name] = bitmap;
        }
    }

    private void ApplyAvatarFrame()
    {
        var name = _expressionState.ResolveAssetName();
        if (!_avatarFrames.TryGetValue(name, out var source)) return;
        if (!ReferenceEquals(AvatarImage.Source, source)) AvatarImage.Source = source;
        if (!ReferenceEquals(PetAvatarImage.Source, source)) PetAvatarImage.Source = source;
    }

    private void RequestExpression(AvatarExpression expression, double holdSeconds = 1.0, bool force = false)
    {
        if (_expressionState.Request(expression, TimeSpan.FromSeconds(holdSeconds), force))
            ApplyAvatarFrame();
    }

    private void StartSpeakingAnimation()
    {
        if (_expressionState.IsSpeaking) return;
        _expressionState.SetSpeaking(true);
        _mouthTimer.Start();
        PlayReaction(ReactionCue.Speaking, false);
        ApplyAvatarFrame();
    }

    private void StopSpeakingAnimation()
    {
        _mouthTimer.Stop();
        _expressionState.SetSpeaking(false);
        ApplyAvatarFrame();
    }

    private async void BlinkTimer_Tick(object? sender, EventArgs e)
    {
        _blinkTimer.Stop();
        _expressionState.SetBlinking(true);
        ApplyAvatarFrame();
        await Task.Delay(115);
        if (_exitRequested) return;
        _expressionState.SetBlinking(false);
        ApplyAvatarFrame();
        ScheduleNextBlink();
        _blinkTimer.Start();
    }

    private void ScheduleNextBlink()
    {
        // Occasionally schedule a quick second blink, while keeping most intervals calm.
        var milliseconds = _animationRandom.NextDouble() < 0.16
            ? _animationRandom.Next(650, 1050)
            : _animationRandom.Next(3200, 7200);
        _blinkTimer.Interval = TimeSpan.FromMilliseconds(milliseconds);
    }

    private void SetConnected(bool connected)
    {
        _connected = connected;
        var connectionBrush = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(
            connected ? "#78A98C" : "#D5A6A6"));
        ConnectionDot.Fill = connectionBrush;
        PetConnectionDot.Fill = connectionBrush;
        HeaderStateText.Text = connected ? "在线陪伴" : "正在重连";
        if (!connected)
        {
            _busy = false;
            StopSpeakingAnimation();
            RequestExpression(AvatarExpression.Worried, 2.5, true);
            InputBox.IsEnabled = true;
            ActivityText.Text = "正在寻找桌面核心…";
            MoodText.Text = "状态：重连中";
            PetStateText.Text = "正在寻找核心…";
            SetWorkingVisual(false);
            PlayReaction(ReactionCue.Error);
            ShowPetBubble("和核心的连接断开了，正在自动重连…", true, BubbleKind.Error, true);
            ShowToast("和核心的连接断开了，正在自动重连…", "#FFF2EE");
        }
        else
        {
            PetStateText.Text = "在线 · 安静陪伴";
            PlayReaction(ReactionCue.Welcome);
            _ = RefreshTaskStateAsync();
            _ = RefreshMemoriesAsync();
            _ = RefreshRemindersAsync();
            _ = RefreshProactiveAsync();
            _ = RefreshProjectsAsync();
            _ = RefreshAutonomyAsync();
            _ = SendPresencePulseAsync();
            _ = _pipe.SendAsync(new { type = "workspace.info", request_id = Guid.NewGuid().ToString("N") });
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
                    var energy = status.TryGetProperty("energy", out var energyElement)
                        ? energyElement.GetDouble() : 0.5;
                    var activeTasks = status.TryGetProperty("tasks", out var taskStats) &&
                        taskStats.TryGetProperty("active", out var activeElement)
                        ? activeElement.GetInt32() : 0;
                    _lastEnergy = energy;
                    _lastActiveTasks = activeTasks;
                    _lastMood = status.TryGetProperty("mood", out var compactMood)
                        ? compactMood.GetString() ?? "平静" : "平静";
                    StateDetailText.Text = $"精力 {energy:P0} · 活跃任务 {activeTasks}";
                    EnergyBar.Value = energy;
                    PetStateText.Text = _busy
                        ? "正在回应主人…"
                        : activeTasks > 0
                        ? $"{_lastMood} · 忙碌中 · {energy:P0}"
                        : $"{_lastMood} · 精力 {energy:P0}";
                    ApplyMoodVisual(_lastMood);
                    RequestExpression(AvatarExpressionStateMachine.FromStatus(
                        _lastMood, energy, activeTasks > 0 || _busy));
                    UpdateAvatarState(energy, activeTasks > 0 || _busy);
                }
                break;
            case "proactive.snapshot":
                if (message.TryGetProperty("snapshot", out var proactiveSnapshot))
                    RenderProactive(proactiveSnapshot);
                break;
            case "proactive.prompt":
                if (message.TryGetProperty("candidate", out var proactiveCandidate))
                    ShowProactivePrompt(proactiveCandidate);
                break;
            case "proactive.details":
                if (message.TryGetProperty("details", out var proactiveDetails))
                    ShowProactiveDetails(proactiveDetails);
                break;
            case "proactive.error":
                ProactiveFeedbackText.Text = message.TryGetProperty("message", out var proactiveError)
                    ? proactiveError.GetString() ?? "主动陪伴设置保存失败" : "主动陪伴设置保存失败";
                break;
            case "projects.snapshot":
                if (message.TryGetProperty("snapshot", out var projectsSnapshot))
                    RenderProjects(projectsSnapshot);
                break;
            case "autonomy.snapshot":
                if (message.TryGetProperty("snapshot", out var autonomySnapshot))
                    RenderAutonomy(autonomySnapshot);
                break;
            case "autonomy.error":
                AutonomyFeedbackText.Text = message.TryGetProperty("message", out var autonomyError)
                    ? autonomyError.GetString() ?? "自主操作失败" : "自主操作失败";
                break;
            case "project.plan_requested":
                _pendingSourceProjectId = message.TryGetProperty("project_id", out var sourceProject)
                    ? sourceProject.GetString() ?? "" : "";
                _pendingSourceOpportunityId = message.TryGetProperty("opportunity_id", out var sourceOpportunity)
                    ? sourceOpportunity.GetString() ?? "" : "";
                var suggestedGoal = message.TryGetProperty("goal", out var suggestedGoalElement)
                    ? suggestedGoalElement.GetString() ?? "" : "";
                GoalTextBox.Text = suggestedGoal;
                SetPetMode(false);
                MainTabs.SelectedIndex = 1;
                TaskFeedbackText.Text = "下一步建议已经放入目标框；主人检查后再生成计划预览。";
                ShowPetBubble("建议已经放进目标框啦，主人确认内容后再让我生成计划。", true, BubbleKind.Speech);
                break;
            case "project.error":
                ProjectFeedbackText.Text = message.TryGetProperty("message", out var projectError)
                    ? projectError.GetString() ?? "项目操作失败" : "项目操作失败";
                break;
            case "chat.started":
                _busy = true;
                StopSpeakingAnimation();
                RequestExpression(AvatarExpression.Focused, 1.2);
                _reply.Clear();
                _bubbleSentenceBuffer.Clear();
                ActivityText.Text = "正在认真听主人说话…";
                HeaderStateText.Text = "正在回应";
                PetStateText.Text = "认真听主人说话…";
                SetWorkingVisual(true);
                PlayReaction(ReactionCue.Listening, false);
                ShowPetBubble("嗯嗯，未名子在认真听主人说话…", false, BubbleKind.Thinking);
                AppendChat("\n\n未名子\n", true);
                break;
            case "chat.token":
                if (message.TryGetProperty("content", out var content))
                {
                    StartSpeakingAnimation();
                    var token = content.GetString() ?? string.Empty;
                    _reply.Append(token);
                    AppendChat(token, false);
                    FeedBubbleToken(token);
                }
                break;
            case "chat.tool":
                StopSpeakingAnimation();
                RequestExpression(AvatarExpression.Focused, 2.0, true);
                ActivityText.Text = "正在帮主人查找资料…";
                PetStateText.Text = "正在查资料…";
                PlayReaction(ReactionCue.Tool);
                ShowPetBubble("我去查一下，主人稍等我一小会儿喵。", false, BubbleKind.Tool);
                break;
            case "chat.done":
                _busy = false;
                StopSpeakingAnimation();
                FlushBubbleSentence();
                var completedReply = _reply.ToString().Trim();
                if (!string.IsNullOrWhiteSpace(completedReply)) RememberBubble(completedReply, BubbleKind.Speech);
                RequestExpression(AvatarExpressionStateMachine.FromStatus(
                    _lastMood, _lastEnergy, _lastActiveTasks > 0));
                InputBox.IsEnabled = true;
                InputBox.Focus();
                HeaderStateText.Text = "在线陪伴";
                PetStateText.Text = _lastActiveTasks > 0 ? "任务处理中" : $"{_lastMood} · 精力 {_lastEnergy:P0}";
                SetWorkingVisual(_lastActiveTasks > 0);
                PlayReaction(ReactionCue.Success);
                if (_petMode && completedReply.Length > 220)
                {
                    SetPetMode(false);
                    MainTabs.SelectedIndex = 0;
                    ShowToast("回复比较长，已经为主人展开完整对话面板。", "#EEF3FF");
                }
                else
                {
                    ShowPetBubble(string.IsNullOrWhiteSpace(_lastBubbleSegment) ? completedReply : _lastBubbleSegment,
                        true, BubbleKind.Speech);
                }
                break;
            case "chat.error":
                _busy = false;
                StopSpeakingAnimation();
                RequestExpression(AvatarExpression.Worried, 3.0, true);
                InputBox.IsEnabled = true;
                var error = message.TryGetProperty("message", out var errorElement)
                    ? errorElement.GetString() : "回复时出了点问题";
                AppendChat($"\n（{error}）", false);
                HeaderStateText.Text = "回复失败";
                SetWorkingVisual(_lastActiveTasks > 0);
                PlayReaction(ReactionCue.Error);
                ShowPetBubble("刚才的连接好像绊了一下……主人可以再试一次吗？", true,
                    BubbleKind.Error, true);
                ShowToast(error ?? "回复时出了点问题", "#FFF0EC");
                InputBox.Focus();
                break;
            case "tasks.snapshot":
                if (message.TryGetProperty("tasks", out var tasks))
                {
                    RenderTasks(tasks);
                    RestoreDraftPlan(tasks);
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
                    RequestExpression(AvatarExpression.Shy, 2.0);
                    PlayReaction(ReactionCue.Permission);
                    ShowPetBubble("主人，有一步操作需要你亲自确认，我会等你的决定。", true,
                        BubbleKind.Permission, true);
                    ShowToast("有一项操作正在等待确认。", "#FFF8E8");
                }
                break;
            case "task.created":
                TaskFeedbackText.Text = "任务已经保存，等待主人确认后才会执行。";
                ShowToast("任务已保存，正在等待确认。", "#EEF3FF");
                if (message.TryGetProperty("approval", out var newApproval))
                {
                    ShowApproval(newApproval);
                }
                _ = RefreshTaskStateAsync();
                break;
            case "goal.planning":
                CreateGoalPlanButton.IsEnabled = false;
                CreateGoalPlanButton.Content = "正在整理计划…";
                TaskFeedbackText.Text = "未名子正在把目标拆成可检查的安全步骤。";
                RequestExpression(AvatarExpression.Focused, 2.0, true);
                SetWorkingVisual(true);
                PlayReaction(ReactionCue.Thinking);
                ShowPetBubble("我正在把目标拆成可以逐步检查的计划。", false, BubbleKind.Thinking);
                ShowToast("正在把目标整理成计划…");
                break;
            case "plan.preview":
                CreateGoalPlanButton.IsEnabled = true;
                CreateGoalPlanButton.Content = "生成计划预览";
                SetWorkingVisual(_lastActiveTasks > 0);
                RequestExpression(AvatarExpression.Happy, 1.8, true);
                PlayReaction(ReactionCue.Success);
                ShowPetBubble("计划整理好了，请主人检查一下。", true, BubbleKind.Speech);
                if (message.TryGetProperty("task", out var planTask))
                {
                    var source = message.TryGetProperty("planner_source", out var sourceElement)
                        ? sourceElement.GetString() : null;
                    ShowPlanPreview(planTask, source);
                }
                _pendingSourceProjectId = string.Empty;
                _pendingSourceOpportunityId = string.Empty;
                break;
            case "plan.notice":
                TaskFeedbackText.Text = message.TryGetProperty("message", out var notice)
                    ? notice.GetString() ?? "已使用本地保守规划" : "已使用本地保守规划";
                break;
            case "plan.metrics":
                if (message.TryGetProperty("metrics", out var metrics))
                {
                    var source = metrics.TryGetProperty("source", out var metricSource)
                        ? metricSource.GetString() ?? "未知" : "未知";
                    var elapsed = metrics.TryGetProperty("elapsed_ms", out var elapsedElement)
                        ? elapsedElement.GetInt64() : 0;
                    var reason = metrics.TryGetProperty("reason", out var reasonElement)
                        ? reasonElement.GetString() ?? "" : "";
                    PlanMetricsText.Text = $"规划：{source} · {elapsed / 1000.0:0.0}s" +
                        (string.IsNullOrWhiteSpace(reason) ? "" : $"\n降级原因：{reason}");
                }
                break;
            case "plan.edited":
                TaskFeedbackText.Text = "草案修改已保存；仍未执行任何写入。";
                if (message.TryGetProperty("task", out var editedPlanTask))
                    ShowPlanPreview(editedPlanTask, "owner_edited");
                break;
            case "plan.confirmed":
                HidePlanPreview();
                TaskFeedbackText.Text = "计划已确认。未名子会逐步推进，并在写入前再次请求许可。";
                ShowPetBubble("计划收好啦，我会一步一步认真做。", true);
                RequestExpression(AvatarExpression.Happy, 2.0, true);
                PlayReaction(ReactionCue.Success);
                _ = RefreshTaskStateAsync();
                break;
            case "plan.rejected":
                HidePlanPreview();
                TaskFeedbackText.Text = "这份计划已经放弃，没有执行任何写入。";
                _ = RefreshTaskStateAsync();
                break;
            case "task.updated":
                ShowToast("任务进度已经更新。", "#EFF8F1");
                _ = RefreshTaskStateAsync();
                break;
            case "task.action_done":
                TaskFeedbackText.Text = "任务状态已经更新。";
                _ = RefreshTaskStateAsync();
                break;
            case "task.queued":
                TaskFeedbackText.Text = "前一个任务仍在执行，这项操作已经进入本地队列。";
                ShowPetBubble("这项任务已经排好队啦，我会按顺序认真完成。", true,
                    BubbleKind.Tool, true);
                PlayReaction(ReactionCue.Working);
                ShowToast("任务已进入本地队列。", "#EEF3FF");
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
                ShowToast(TaskFeedbackText.Text, "#FFF0EC");
                RequestExpression(AvatarExpression.Worried, 2.5, true);
                PlayReaction(ReactionCue.Error);
                ShowPetBubble(TaskFeedbackText.Text, true, BubbleKind.Error, true);
                break;
            case "plan.error":
                CreateGoalPlanButton.IsEnabled = true;
                CreateGoalPlanButton.Content = "生成计划预览";
                SetWorkingVisual(_lastActiveTasks > 0);
                TaskFeedbackText.Text = message.TryGetProperty("message", out var planError)
                    ? planError.GetString() ?? "计划生成失败"
                    : "计划生成失败";
                ShowToast(TaskFeedbackText.Text, "#FFF0EC");
                RequestExpression(AvatarExpression.Worried, 2.5, true);
                PlayReaction(ReactionCue.Error);
                ShowPetBubble(TaskFeedbackText.Text, true, BubbleKind.Error, true);
                break;
            case "workspace.info":
                _workspacePath = message.TryGetProperty("path", out var workspacePath)
                    ? workspacePath.GetString() ?? string.Empty : string.Empty;
                break;
            case "reminders.snapshot":
                if (message.TryGetProperty("reminders", out var reminders)) RenderReminders(reminders);
                break;
            case "reminder.created":
                TaskFeedbackText.Text = "提醒已经保存，核心重启后仍会保留。";
                PlayReaction(ReactionCue.Success);
                ShowToast("提醒已经记下来了。", "#EFF8F1");
                _ = RefreshRemindersAsync();
                break;
            case "reminder.cancelled":
                TaskFeedbackText.Text = "选中的提醒已经取消。";
                _ = RefreshRemindersAsync();
                break;
            case "reminder.due":
                if (message.TryGetProperty("reminder", out var dueReminder))
                {
                    var dueTitle = dueReminder.TryGetProperty("title", out var dueTitleElement)
                        ? dueTitleElement.GetString() ?? "主人建立的提醒" : "主人建立的提醒";
                    var dueMessage = dueReminder.TryGetProperty("message", out var dueMessageElement)
                        ? dueMessageElement.GetString() ?? "到时间啦" : "到时间啦";
                    _tray.BalloonTipTitle = dueTitle;
                    _tray.BalloonTipText = dueMessage;
                    _tray.ShowBalloonTip(8000);
                    AppendChat($"\n\n未名子 · 提醒\n{dueMessage}", true);
                    RequestExpression(AvatarExpression.Happy, 2.2, true);
                    PlayReaction(ReactionCue.Reminder);
                    ShowPetBubble(dueMessage, true, BubbleKind.Reminder, true);
                    ShowToast(dueMessage, "#FFF8E8");
                    _ = RefreshRemindersAsync();
                }
                break;
            case "memories.snapshot":
                if (message.TryGetProperty("memories", out var memories)) RenderMemories(memories);
                break;
            case "memory.updated":
                MemoryFeedbackText.Text = "记忆已经更新。";
                RequestExpression(AvatarExpression.Happy, 1.6);
                PlayReaction(ReactionCue.Success);
                ShowToast("这条记忆已经更新。", "#EFF8F1");
                _ = RefreshMemoriesAsync();
                break;
            case "memory.forgotten":
                MemoryFeedbackText.Text = "这条记忆已经彻底遗忘。";
                _ = RefreshMemoriesAsync();
                break;
            case "memory.error":
                MemoryFeedbackText.Text = message.TryGetProperty("message", out var memoryError)
                    ? memoryError.GetString() ?? "记忆操作失败" : "记忆操作失败";
                break;
            case "perception.started":
                PerceptionFeedbackText.Text = "正在识别主人授权的这一张窗口截图…";
                RequestExpression(AvatarExpression.Focused, 2.0, true);
                SetWorkingVisual(true);
                PlayReaction(ReactionCue.Tool);
                ShowPetBubble("正在认真看主人刚刚授权的这一张截图…", false, BubbleKind.Tool);
                ShowToast("正在识别这一次授权的截图…", "#EEF3FF");
                break;
            case "perception.result":
                var perceptionSource = message.TryGetProperty("source", out var perceptionSourceJson)
                    ? perceptionSourceJson.GetString() ?? "once" : "once";
                if (perceptionSource == "observation") _observationVisionInFlight = false;
                var perceptionText = message.TryGetProperty("context", out var contextElement)
                    ? contextElement.GetString() ?? "" : "";
                var directRequestId = message.TryGetProperty("request_id", out var directRequestJson)
                    ? directRequestJson.GetString() ?? "" : "";
                TaskCompletionSource<string>? directCompletion = null;
                var directHandled = perceptionSource == "direct" &&
                    _directPerceptionRequests.Remove(directRequestId, out directCompletion);
                if (directHandled && directCompletion is not null)
                {
                    directCompletion.TrySetResult(perceptionText);
                }
                else if (!string.IsNullOrWhiteSpace(perceptionText))
                {
                    AddPerceptionContext(perceptionText);
                }
                PerceptionFeedbackText.Text = message.TryGetProperty("message", out var perceptionMessage)
                    ? perceptionMessage.GetString() ?? "截图识别完成" : "截图识别完成";
                SetWorkingVisual(_lastActiveTasks > 0 || _busy);
                RequestExpression(AvatarExpression.Happy, 1.8, true);
                PlayReaction(ReactionCue.Success);
                ShowPetBubble(perceptionSource == "observation"
                        ? "我注意到窗口有明显变化，已经把看到的内容放进预览啦。"
                        : "已经看完啦，识别结果会跟着下一条消息一起发送。", true,
                    BubbleKind.Speech);
                ShowToast("截图识别完成，内容已加入本次上下文。", "#EFF8F1");
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

    private async Task RefreshMemoriesAsync()
    {
        if (!_connected) return;
        var selected = MemoryStatusBox.SelectedItem as System.Windows.Controls.ComboBoxItem;
        var status = selected?.Tag?.ToString() ?? "active";
        try
        {
            await _pipe.SendAsync(new
            {
                type = "memories.list",
                request_id = Guid.NewGuid().ToString("N"),
                status,
            });
        }
        catch (Exception ex)
        {
            MemoryFeedbackText.Text = ex.Message;
        }
    }

    private async Task RefreshProactiveAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new { type = "proactive.status", request_id = Guid.NewGuid().ToString("N") });
        }
        catch (Exception ex)
        {
            ProactiveFeedbackText.Text = ex.Message;
        }
    }

    private async Task RefreshProjectsAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new { type = "projects.status", request_id = Guid.NewGuid().ToString("N") });
        }
        catch (Exception ex)
        {
            ProjectFeedbackText.Text = ex.Message;
        }
    }

    private async Task RefreshAutonomyAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new { type = "autonomy.status", request_id = Guid.NewGuid().ToString("N") });
        }
        catch (Exception ex)
        {
            AutonomyFeedbackText.Text = ex.Message;
        }
    }

    private void RenderAutonomy(JsonElement snapshot)
    {
        var enabled = snapshot.TryGetProperty("enabled", out var enabledElement) && enabledElement.GetBoolean();
        _autonomyPaused = snapshot.TryGetProperty("paused", out var pausedElement) && pausedElement.GetBoolean();
        _autonomyDraftsDir = snapshot.TryGetProperty("drafts_dir", out var draftsElement)
            ? draftsElement.GetString() ?? "" : "";
        var activeGrants = snapshot.TryGetProperty("active_grant_count", out var activeElement) ? activeElement.GetInt32() : 0;
        var queued = snapshot.TryGetProperty("queued_count", out var queuedElement) ? queuedElement.GetInt32() : 0;
        var drafts = snapshot.TryGetProperty("draft_count", out var draftElement) ? draftElement.GetInt32() : 0;
        var createdToday = snapshot.TryGetProperty("created_today", out var todayElement) ? todayElement.GetInt32() : 0;
        var dailyLimit = snapshot.TryGetProperty("daily_limit", out var limitElement) ? limitElement.GetInt32() : 3;
        var unread = snapshot.TryGetProperty("unread_inbox_count", out var unreadElement) ? unreadElement.GetInt32() : 0;
        var activeIntents = snapshot.TryGetProperty("active_intent_count", out var intentCountElement) ? intentCountElement.GetInt32() : 0;
        var activePackages = snapshot.TryGetProperty("active_package_count", out var packageCountElement) ? packageCountElement.GetInt32() : 0;
        AutonomySummaryText.Text = !enabled ? "尚未授权 · 不会自主创建文件"
            : _autonomyPaused ? $"已暂停 · {activeGrants} 张有效能力卡"
            : $"运行中 · {activeGrants} 张能力卡 · {activePackages} 个权限包 · 意图 {activeIntents} · 队列 {queued} · 可采纳 {drafts} · 未读 {unread}";
        AutonomyBoundaryText.Text = $"今天已创建 {createdToday}/{dailyLimit} · V6 情境→意图→权限→行动→复核 · 所有边界在本地验证";
        PauseAutonomyButton.Content = _autonomyPaused ? "恢复全部" : "暂停全部";

        AutonomyIntentText.Text = "当前没有形成新的自主意图。";
        if (snapshot.TryGetProperty("intents", out var intentArray) && intentArray.ValueKind == JsonValueKind.Array)
        {
            var intent = intentArray.EnumerateArray().FirstOrDefault();
            if (intent.ValueKind == JsonValueKind.Object)
            {
                var status = intent.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "" : "";
                var project = intent.TryGetProperty("project_title", out var projectElement) ? projectElement.GetString() ?? "项目" : "项目";
                var title = intent.TryGetProperty("title", out var titleElement) ? titleElement.GetString() ?? "下一步" : "下一步";
                var why = intent.TryGetProperty("why_now", out var whyElement) ? whyElement.GetString() ?? "" : "";
                var benefit = intent.TryGetProperty("expected_benefit", out var benefitElement) ? benefitElement.GetString() ?? "" : "";
                var expression = intent.TryGetProperty("expression_hint", out var expressionElement) ? expressionElement.GetString() ?? "" : "";
                var risk = intent.TryGetProperty("risk", out var riskElement) ? riskElement.GetString() ?? "" : "";
                var grant = intent.TryGetProperty("grant_name", out var grantElement) ? grantElement.GetString() ?? "能力卡" : "能力卡";
                AutonomyIntentText.Text = $"{IntentStatusText(status)} · {project}\n想做：{title}\n为什么现在：{why}\n预期帮助：{benefit}\n此刻的样子：{expression}\n权限：{grant}\n风险边界：{risk}";
            }
        }

        var selectedPackageId = (AutonomyPackageListBox.SelectedItem as AutonomyPackageViewItem)?.Id;
        var packages = new List<AutonomyPackageViewItem>();
        if (snapshot.TryGetProperty("packages", out var packageArray) && packageArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var package in packageArray.EnumerateArray())
            {
                var id = package.TryGetProperty("package_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var status = package.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "" : "";
                var name = package.TryGetProperty("name", out var nameElement) ? nameElement.GetString() ?? "委托权限包" : "委托权限包";
                var projectId = package.TryGetProperty("project_id", out var projectElement) ? projectElement.GetString() ?? "" : "";
                var expires = package.TryGetProperty("expires_at", out var expiresElement) ? expiresElement.GetDouble() : 0;
                var expiresText = expires > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)expires).LocalDateTime.ToString("MM-dd") : "--";
                packages.Add(new AutonomyPackageViewItem {
                    Id = id, Status = status,
                    Display = $"{(status == "active" ? "有效" : status)} · {name} · {(string.IsNullOrWhiteSpace(projectId) ? "轻量全局" : "指定项目")} · 至 {expiresText}"
                });
            }
        }
        AutonomyPackageListBox.ItemsSource = packages;
        AutonomyPackageListBox.SelectedItem = packages.FirstOrDefault(item => item.Id == selectedPackageId) ?? packages.FirstOrDefault();
        RevokePackageButton.IsEnabled = AutonomyPackageListBox.SelectedItem is AutonomyPackageViewItem packageItem && packageItem.Status == "active";

        var selectedGrantId = (AutonomyGrantListBox.SelectedItem as AutonomyGrantViewItem)?.Id;
        var grants = new List<AutonomyGrantViewItem>();
        var grantHistory = new List<AutonomyGrantViewItem>();
        if (snapshot.TryGetProperty("grants", out var grantArray) && grantArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var grant in grantArray.EnumerateArray())
            {
                var id = grant.TryGetProperty("grant_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var status = grant.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "" : "";
                var level = grant.TryGetProperty("level", out var levelElement) ? levelElement.GetString() ?? "L1" : "L1";
                var projectId = grant.TryGetProperty("project_id", out var projectElement) ? projectElement.GetString() ?? "" : "";
                var expires = grant.TryGetProperty("expires_at", out var expiresElement) ? expiresElement.GetDouble() : 0;
                var expiresText = expires > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)expires).LocalDateTime.ToString("MM-dd") : "--";
                var statusText = status switch { "active" => "有效", "revoked" => "已撤销", "expired" => "已过期", _ => status };
                var item = new AutonomyGrantViewItem {
                    Id = id, ProjectId = projectId, Status = status,
                    Display = $"{level} · {(string.IsNullOrWhiteSpace(projectId) ? "全部项目" : "指定项目")} · {statusText} · 至 {expiresText}"
                };
                if (status == "active") grants.Add(item); else grantHistory.Add(item);
            }
        }
        AutonomyGrantListBox.ItemsSource = grants;
        AutonomyGrantListBox.SelectedItem = grants.FirstOrDefault(item => item.Id == selectedGrantId) ?? grants.FirstOrDefault();
        AutonomyGrantHistoryListBox.ItemsSource = grantHistory;
        AutonomyGrantHistoryExpander.Header = $"历史能力卡（{grantHistory.Count}）";
        AutonomyGrantHistoryExpander.Visibility = grantHistory.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        AutonomyGrantEmptyText.Visibility = grants.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        RevokeAutonomyButton.IsEnabled = AutonomyGrantListBox.SelectedItem is AutonomyGrantViewItem;

        var selectedJobId = (AutonomyJobListBox.SelectedItem as AutonomyJobViewItem)?.Id;
        var jobs = new List<AutonomyJobViewItem>();
        if (snapshot.TryGetProperty("jobs", out var jobArray) && jobArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var job in jobArray.EnumerateArray())
            {
                var id = job.TryGetProperty("job_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var status = job.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "" : "";
                var title = job.TryGetProperty("title", out var titleElement) ? titleElement.GetString() ?? "未命名草稿" : "未命名草稿";
                var project = job.TryGetProperty("project_title", out var projectElement) ? projectElement.GetString() ?? "未命名项目" : "未命名项目";
                var path = job.TryGetProperty("draft_path", out var pathElement) ? pathElement.GetString() ?? "" : "";
                var error = job.TryGetProperty("error", out var errorElement) ? errorElement.GetString() ?? "" : "";
                var valueScore = job.TryGetProperty("value_score", out var scoreElement) ? scoreElement.GetDouble() : 0;
                var reviewScore = job.TryGetProperty("review", out var reviewElement) && reviewElement.ValueKind == JsonValueKind.Object &&
                    reviewElement.TryGetProperty("score", out var reviewScoreElement) ? reviewScoreElement.GetDouble() : 0;
                var goalId = job.TryGetProperty("goal_id", out var goalElement) ? goalElement.GetString() ?? "" : "";
                var diff = job.TryGetProperty("diff_preview", out var diffElement) ? diffElement.GetString() ?? "" : "";
                var goalDetail = "";
                if (job.TryGetProperty("goal", out var goalData) && goalData.ValueKind == JsonValueKind.Object)
                {
                    var why = goalData.TryGetProperty("why_now", out var whyElement) ? whyElement.GetString() ?? "" : "";
                    var criteria = new List<string>();
                    if (goalData.TryGetProperty("completion_criteria", out var criteriaArray) && criteriaArray.ValueKind == JsonValueKind.Array)
                        criteria.AddRange(criteriaArray.EnumerateArray().Select(item => item.GetString() ?? ""));
                    var subgoals = new List<string>();
                    if (goalData.TryGetProperty("subgoals", out var subgoalArray) && subgoalArray.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var subgoal in subgoalArray.EnumerateArray())
                        {
                            var name = subgoal.TryGetProperty("name", out var nameElement) ? nameElement.GetString() ?? "" : "";
                            var subStatus = subgoal.TryGetProperty("status", out var subStatusElement) ? subStatusElement.GetString() ?? "" : "";
                            subgoals.Add($"{name}（{subStatus}）");
                        }
                    }
                    goalDetail = $"\n为什么现在做：{why}\n子目标：{string.Join(" → ", subgoals)}\n完成标准：{string.Join("；", criteria)}";
                }
                var checks = new List<string>();
                if (job.TryGetProperty("validation", out var checkArray) && checkArray.ValueKind == JsonValueKind.Array)
                    checks.AddRange(checkArray.EnumerateArray().Select(item => item.GetString() ?? ""));
                jobs.Add(new AutonomyJobViewItem {
                    Id = id, Status = status, Path = path,
                    Display = $"{AutonomyStatusText(status)} · {project}\n{title}",
                    Detail = $"状态：{AutonomyStatusText(status)}\n目标：{(string.IsNullOrWhiteSpace(goalId) ? "旧版工作" : goalId[..Math.Min(8, goalId.Length)])}{goalDetail}\n价值评分：{valueScore:P0} · 自我复核：{reviewScore:P0}\n路径：{(string.IsNullOrWhiteSpace(path) ? "尚未生成" : path)}\n验证：{(checks.Count == 0 ? "等待验证" : string.Join("、", checks))}{(string.IsNullOrWhiteSpace(diff) ? "" : "\n已生成只读差异预览，不会自动应用。")}{(string.IsNullOrWhiteSpace(error) ? "" : $"\n说明：{error}")}",
                });
            }
        }
        AutonomyJobListBox.ItemsSource = jobs;
        AutonomyJobListBox.SelectedItem = jobs.FirstOrDefault(item => item.Id == selectedJobId) ?? jobs.FirstOrDefault();

        var inbox = new List<AutonomyInboxViewItem>();
        if (snapshot.TryGetProperty("inbox", out var inboxArray) && inboxArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in inboxArray.EnumerateArray())
            {
                var id = item.TryGetProperty("inbox_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var status = item.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "unread" : "unread";
                var project = item.TryGetProperty("project_title", out var projectElement) ? projectElement.GetString() ?? "项目" : "项目";
                var message = item.TryGetProperty("message", out var messageElement) ? messageElement.GetString() ?? "" : "";
                var reason = item.TryGetProperty("reason", out var reasonElement) ? reasonElement.GetString() ?? "" : "";
                var score = item.TryGetProperty("value_score", out var scoreElement) ? scoreElement.GetDouble() : 0;
                var intentId = item.TryGetProperty("intent_id", out var intentElement) ? intentElement.GetString() ?? "" : "";
                var packageId = item.TryGetProperty("package_id", out var packageElement) ? packageElement.GetString() ?? "" : "";
                var reviewState = item.TryGetProperty("post_action_review", out var reviewStateElement) ? reviewStateElement.GetString() ?? "pending" : "pending";
                inbox.Add(new AutonomyInboxViewItem {
                    Id = id, Display = $"{(status == "unread" ? "●" : "○")} {project}\n{message}",
                    Detail = $"{message}\n\n主动依据：{reason}\n价值评分：{score:P0}\n意图：{ShortId(intentId)} · 权限包：{(string.IsNullOrWhiteSpace(packageId) ? "未使用" : ShortId(packageId))}\n事后确认：{(reviewState == "pending" ? "等待主人查看或反馈" : "已记录反馈")}"
                });
            }
        }
        AutonomyInboxListBox.ItemsSource = inbox;
        AutonomyInboxListBox.SelectedItem = inbox.FirstOrDefault();

        var decisions = new List<AutonomyDecisionViewItem>();
        if (snapshot.TryGetProperty("decisions", out var decisionArray) && decisionArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in decisionArray.EnumerateArray().Take(30))
            {
                var status = item.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "" : "";
                var title = item.TryGetProperty("title", out var titleElement) ? titleElement.GetString() ?? "" : "";
                var reason = item.TryGetProperty("reason", out var reasonElement) ? reasonElement.GetString() ?? "" : "";
                var score = item.TryGetProperty("score", out var scoreElement) ? scoreElement.GetDouble() : 0;
                decisions.Add(new AutonomyDecisionViewItem { Display = $"{status} · {score:P0} · {title}\n{reason}" });
            }
        }
        AutonomyDecisionListBox.ItemsSource = decisions;
        if (snapshot.TryGetProperty("costs", out var costs) && costs.ValueKind == JsonValueKind.Object)
        {
            var requests = costs.TryGetProperty("network_requests", out var requestsElement) ? requestsElement.GetInt32() : 0;
            var bytes = costs.TryGetProperty("network_bytes", out var bytesElement) ? bytesElement.GetInt32() : 0;
            var tokens = costs.TryGetProperty("model_tokens", out var tokensElement) ? tokensElement.GetInt32() : 0;
            var explanation = costs.TryGetProperty("explanation", out var explanationElement) ? explanationElement.GetString() ?? "" : "";
            AutonomyCostText.Text = $"今天网络读取 {requests} 次 / {bytes / 1024.0:F1} KB · 模型 Token {tokens}\n{explanation}";
        }

        var trustEntries = snapshot.TryGetProperty("trust", out var trustObject) && trustObject.ValueKind == JsonValueKind.Object
            ? trustObject.EnumerateObject().ToList() : [];
        var trusted = trustEntries.Count(item => item.Value.TryGetProperty("level", out var level) && level.GetString() == "trusted_within_grant");
        var restricted = trustEntries.Count(item => item.Value.TryGetProperty("level", out var level) && level.GetString() == "restricted");
        var circuitStatus = "closed";
        var circuitReason = "";
        if (snapshot.TryGetProperty("circuit", out var circuit) && circuit.ValueKind == JsonValueKind.Object)
        {
            circuitStatus = circuit.TryGetProperty("status", out var circuitStatusElement) ? circuitStatusElement.GetString() ?? "closed" : "closed";
            circuitReason = circuit.TryGetProperty("reason", out var reasonElement) ? reasonElement.GetString() ?? "" : "";
        }
        AutonomyTrustText.Text = $"信任学习：授权内可信 {trusted} 类 · 收紧 {restricted} 类；信任只调节频率，不会新增权限。\n熔断：{CircuitStatusText(circuitStatus)}{(string.IsNullOrWhiteSpace(circuitReason) ? "" : $" · {circuitReason}")}";
        ResetCircuitButton.Visibility = circuitStatus is "open" or "half_open" ? Visibility.Visible : Visibility.Collapsed;
        var journal = new List<AutonomyAuditViewItem>();
        if (snapshot.TryGetProperty("life_journal", out var journalArray) && journalArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in journalArray.EnumerateArray().Take(30))
            {
                var when = item.TryGetProperty("time_text", out var whenElement) ? whenElement.GetString() ?? "" : "";
                var summary = item.TryGetProperty("summary", out var summaryElement) ? summaryElement.GetString() ?? "" : "";
                journal.Add(new AutonomyAuditViewItem { Display = $"{when} · {summary}" });
            }
        }
        AutonomyLifeJournalListBox.ItemsSource = journal;

        var audit = new List<AutonomyAuditViewItem>();
        if (snapshot.TryGetProperty("audit", out var auditArray) && auditArray.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in auditArray.EnumerateArray().Take(30))
            {
                var summary = item.TryGetProperty("summary", out var summaryElement) ? summaryElement.GetString() ?? "" : "";
                var at = item.TryGetProperty("at", out var atElement) ? atElement.GetDouble() : 0;
                var when = at > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)at).LocalDateTime.ToString("MM-dd HH:mm") : "";
                audit.Add(new AutonomyAuditViewItem { Display = $"{when} · {summary}" });
            }
        }
        AutonomyAuditListBox.ItemsSource = audit;
    }

    private static string AutonomyStatusText(string status) => status switch
    {
        "queued" => "待处理", "generating" => "生成中", "validating" => "验证中",
        "completed" => "可采纳", "awaiting_adoption" => "等待权限确认", "adopted" => "已采纳",
        "discarded" => "已移到可恢复区", "failed" => "失败", "cancelled" => "已取消", _ => status,
    };

    private static string IntentStatusText(string status) => status switch
    {
        "proposed" => "形成想法", "authorized" => "权限允许", "executing" => "正在行动",
        "completed" => "已经完成", "failed" => "行动失败", "blocked" => "暂时停下",
        "cancelled" => "主人已取消", _ => status,
    };

    private static string CircuitStatusText(string status) => status switch
    {
        "closed" => "正常", "open" => "已自动暂停", "half_open" => "等待一次安全试探", _ => status,
    };

    private static string ShortId(string value) => string.IsNullOrWhiteSpace(value)
        ? "未关联" : value[..Math.Min(8, value.Length)];

    private void RenderProjects(JsonElement snapshot)
    {
        var selectedProjectId = !string.IsNullOrWhiteSpace(_pendingEvidenceProjectId)
            ? _pendingEvidenceProjectId
            : (ProjectListBox.SelectedItem as ProjectViewItem)?.Id;
        var projects = new List<ProjectViewItem>();
        if (snapshot.TryGetProperty("projects", out var projectsJson) && projectsJson.ValueKind == JsonValueKind.Array)
        {
            foreach (var project in projectsJson.EnumerateArray())
            {
                var id = project.TryGetProperty("project_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var title = project.TryGetProperty("title", out var titleElement) ? titleElement.GetString() ?? "未命名项目" : "未命名项目";
                var goal = project.TryGetProperty("goal", out var goalElement) ? goalElement.GetString() ?? "" : "";
                var status = project.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "active" : "active";
                var progress = project.TryGetProperty("progress_text", out var progressElement) ? progressElement.GetString() ?? "" : "";
                var archived = project.TryGetProperty("archived", out var archivedElement) && archivedElement.GetBoolean();
                var artifacts = new List<ProjectArtifactViewItem>();
                if (project.TryGetProperty("artifacts", out var artifactsJson) && artifactsJson.ValueKind == JsonValueKind.Array)
                {
                    foreach (var artifact in artifactsJson.EnumerateArray())
                    {
                        var path = artifact.TryGetProperty("path", out var pathElement) ? pathElement.GetString() ?? "" : "";
                        var role = artifact.TryGetProperty("role", out var roleElement) ? roleElement.GetString() ?? "file" : "file";
                        if (!string.IsNullOrWhiteSpace(path))
                            artifacts.Add(new ProjectArtifactViewItem { Path = path, Role = ProjectArtifactRoleText(role) });
                    }
                }
                var opportunities = new List<ProjectOpportunityViewItem>();
                if (project.TryGetProperty("open_opportunities", out var opportunitiesJson) && opportunitiesJson.ValueKind == JsonValueKind.Array)
                {
                    foreach (var opportunity in opportunitiesJson.EnumerateArray())
                    {
                        var opportunityId = opportunity.TryGetProperty("opportunity_id", out var opportunityIdElement)
                            ? opportunityIdElement.GetString() ?? "" : "";
                        var opportunityTitle = opportunity.TryGetProperty("title", out var opportunityTitleElement)
                            ? opportunityTitleElement.GetString() ?? "下一步建议" : "下一步建议";
                        var rationale = opportunity.TryGetProperty("rationale", out var rationaleElement)
                            ? rationaleElement.GetString() ?? "" : "";
                        var risk = opportunity.TryGetProperty("risk", out var riskElement) ? riskElement.GetString() ?? "" : "";
                        var goalText = opportunity.TryGetProperty("proposed_goal", out var proposedGoalElement)
                            ? proposedGoalElement.GetString() ?? "" : "";
                        var opportunityStatus = opportunity.TryGetProperty("status", out var opportunityStatusElement)
                            ? opportunityStatusElement.GetString() ?? "proposed" : "proposed";
                        var valueScore = opportunity.TryGetProperty("value_score", out var valueScoreElement)
                            ? valueScoreElement.GetDouble() : 0;
                        var evidence = new List<string>();
                        if (opportunity.TryGetProperty("evidence", out var evidenceJson) && evidenceJson.ValueKind == JsonValueKind.Array)
                            evidence.AddRange(evidenceJson.EnumerateArray().Select(value => value.GetString() ?? ""));
                        opportunities.Add(new ProjectOpportunityViewItem
                        {
                            Id = opportunityId, ProjectId = id, Title = opportunityTitle,
                            Rationale = rationale, Risk = risk, Goal = goalText,
                            Evidence = string.Join("\n", evidence.Where(value => !string.IsNullOrWhiteSpace(value))),
                            ValueScore = valueScore,
                            Status = opportunityStatus switch { "later" => "已延期", "accepted" => "准备生成计划", _ => "待决定" },
                        });
                    }
                }
                projects.Add(new ProjectViewItem
                {
                    Id = id, Title = title, Goal = goal,
                    Status = ProjectStatusText(status), Progress = progress, Archived = archived,
                    Artifacts = artifacts, Opportunities = opportunities,
                });
            }
        }
        ProjectListBox.ItemsSource = projects;
        ProjectListBox.SelectedItem = projects.FirstOrDefault(item => item.Id == selectedProjectId) ?? projects.FirstOrDefault();
        if (ProjectListBox.SelectedItem is ProjectViewItem evidenceProject &&
            !string.IsNullOrWhiteSpace(_pendingEvidenceOpportunityId))
        {
            ProjectOpportunityListBox.SelectedItem = evidenceProject.Opportunities.FirstOrDefault(
                item => item.Id == _pendingEvidenceOpportunityId) ?? evidenceProject.Opportunities.FirstOrDefault();
            ProjectOpportunityListBox.ScrollIntoView(ProjectOpportunityListBox.SelectedItem);
            _pendingEvidenceProjectId = string.Empty;
            _pendingEvidenceOpportunityId = string.Empty;
        }
        var activeCount = snapshot.TryGetProperty("active_count", out var activeCountElement) ? activeCountElement.GetInt32() : 0;
        var opportunityCount = snapshot.TryGetProperty("open_opportunity_count", out var opportunityCountElement)
            ? opportunityCountElement.GetInt32() : 0;
        ProjectSummaryText.Text = projects.Count == 0
            ? "目前还没有项目；建立目标或任务后会自动形成连续档案。"
            : $"共 {projects.Count} 个项目 · 进行中 {activeCount} · 待决定建议 {opportunityCount}";
    }

    private static string ProjectStatusText(string status) => status switch
    {
        "planning" => "规划中", "waiting_approval" => "等待权限", "running" => "执行中",
        "paused" => "已暂停", "blocked" => "被阻塞", "completed" => "主体已完成",
        "cancelled" => "已取消", "archived" => "已归档", _ => "进行中",
    };

    private static string ProjectArtifactRoleText(string role) => role switch
    {
        "presentation" => "演示文稿", "document" => "文档", "spreadsheet" => "表格",
        "pdf" => "PDF", "data" => "数据", _ => "文件",
    };

    private void RenderProactive(JsonElement snapshot)
    {
        var selectedLoopId = (ProactiveListBox.SelectedItem as ProactiveViewItem)?.LoopId;
        var enabled = snapshot.TryGetProperty("enabled", out var enabledElement) && enabledElement.GetBoolean();
        var used = snapshot.TryGetProperty("used_today", out var usedElement) ? usedElement.GetInt32() : 0;
        var budget = snapshot.TryGetProperty("daily_budget", out var budgetElement) ? budgetElement.GetInt32() : 3;
        var quiet = snapshot.TryGetProperty("quiet_now", out var quietElement) && quietElement.GetBoolean();
        var temporaryQuiet = snapshot.TryGetProperty("temporary_quiet_until", out var temporaryQuietElement)
            ? temporaryQuietElement.GetDouble() : 0;
        var temporaryQuietText = snapshot.TryGetProperty("temporary_quiet_text", out var temporaryQuietTextElement)
            ? temporaryQuietTextElement.GetString() ?? "" : "";
        var next = snapshot.TryGetProperty("next_allowed_text", out var nextElement)
            ? nextElement.GetString() ?? "现在" : "现在";
        var muted = snapshot.TryGetProperty("muted_count", out var mutedElement) ? mutedElement.GetInt32() : 0;
        ProactiveEnabledBox.IsChecked = enabled;
        foreach (var item in ProactiveBudgetBox.Items.OfType<System.Windows.Controls.ComboBoxItem>())
            if (item.Tag?.ToString() == budget.ToString()) ProactiveBudgetBox.SelectedItem = item;
        ProactiveSummaryText.Text = enabled
            ? $"主动陪伴已开启 · 今日 {used}/{budget} 次"
            : "主动陪伴已暂停";
        ProactiveTimingText.Text = temporaryQuiet > DateTimeOffset.Now.ToUnixTimeSeconds()
            ? $"临时安静到 {temporaryQuietText}"
            : quiet ? $"现在处于静默时间 · 下次允许：{next}" : $"下次允许主动出现：{next}";
        var items = new List<ProactiveViewItem>();
        if (snapshot.TryGetProperty("open_loops", out var openLoops) && openLoops.ValueKind == JsonValueKind.Array)
        {
            foreach (var loop in openLoops.EnumerateArray())
            {
                var loopId = loop.TryGetProperty("loop_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var content = loop.TryGetProperty("content", out var contentElement) ? contentElement.GetString() ?? "" : "";
                var status = loop.TryGetProperty("status", out var statusElement) ? statusElement.GetString() ?? "waiting" : "waiting";
                var due = loop.TryGetProperty("next_followup_at", out var dueElement) ? dueElement.GetDouble() : 0;
                var dueText = due > 0
                    ? $"预计 {DateTimeOffset.FromUnixTimeSeconds((long)due).LocalDateTime:MM-dd HH:mm}"
                    : "长期愿望，不自动追问";
                items.Add(new ProactiveViewItem
                {
                    LoopId = loopId, Content = content, DueText = dueText,
                    Status = status switch
                    {
                        "postponed" => "已延期", "awaiting_resolution" => "等待结果",
                        "observed" => "长期保留", _ => "等待回访",
                    },
                });
            }
        }
        ProactiveListBox.ItemsSource = items;
        ProactiveListBox.SelectedItem = items.FirstOrDefault(item => item.LoopId == selectedLoopId) ?? items.FirstOrDefault();
        ProactiveEmptyText.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        var pendingReason = "";
        if (snapshot.TryGetProperty("pending", out var pending) && pending.ValueKind == JsonValueKind.Array && pending.GetArrayLength() > 0)
        {
            var first = pending[0];
            pendingReason = first.TryGetProperty("reason", out var reasonElement) ? reasonElement.GetString() ?? "" : "";
        }
        ProactiveReasonText.Text = string.IsNullOrWhiteSpace(pendingReason)
            ? "未名子只等待真实事件，不会为了显得主动而凭空打扰主人。"
            : $"当前最高优先候选：{pendingReason}";
        var suppression = new List<string>();
        if (snapshot.TryGetProperty("suppression_reasons", out var suppressionJson) && suppressionJson.ValueKind == JsonValueKind.Array)
            suppression.AddRange(suppressionJson.EnumerateArray().Select(value => value.GetString() ?? "").Where(value => !string.IsNullOrWhiteSpace(value)));
        ProactiveSuppressionText.Text = suppression.Count == 0
            ? "现在没有额外的打扰抑制条件。"
            : $"暂不出现：{string.Join("；", suppression)}";
        ProactiveHabitText.Text = snapshot.TryGetProperty("habit_summary", out var habitElement)
            ? habitElement.GetString() ?? "" : "";
        var timelineItems = new List<ProactiveTimelineViewItem>();
        if (snapshot.TryGetProperty("timeline", out var timeline) && timeline.ValueKind == JsonValueKind.Array)
        {
            foreach (var entry in timeline.EnumerateArray().Take(20))
            {
                var summary = entry.TryGetProperty("summary", out var summaryElement) ? summaryElement.GetString() ?? "" : "";
                var at = entry.TryGetProperty("at", out var atElement) ? atElement.GetDouble() : 0;
                var when = at > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)at).LocalDateTime.ToString("MM-dd HH:mm") : "";
                timelineItems.Add(new ProactiveTimelineViewItem { Display = $"{when}　{summary}" });
            }
        }
        ProactiveTimelineListBox.ItemsSource = timelineItems;
        var styleUsage = snapshot.TryGetProperty("style_usage", out var styleUsageElement) ? styleUsageElement : default;
        var calls = styleUsage.ValueKind == JsonValueKind.Object && styleUsage.TryGetProperty("calls", out var callsElement)
            ? callsElement.GetInt32() : 0;
        var promptTokens = styleUsage.ValueKind == JsonValueKind.Object && styleUsage.TryGetProperty("prompt_tokens", out var promptTokenElement)
            ? promptTokenElement.GetInt32() : 0;
        var completionTokens = styleUsage.ValueKind == JsonValueKind.Object && styleUsage.TryGetProperty("completion_tokens", out var completionTokenElement)
            ? completionTokenElement.GetInt32() : 0;
        ProactiveFeedbackText.Text = (muted > 0 ? $"{muted} 个事项已设为不再提醒 · " : "") +
            $"自然表达调用 {calls} 次，累计约 {promptTokens + completionTokens} Token。";
    }

    private void ShowProactivePrompt(JsonElement candidate)
    {
        _currentProactiveCandidateId = candidate.TryGetProperty("id", out var idElement)
            ? idElement.GetString() ?? "" : "";
        _currentProactiveProjectId = candidate.TryGetProperty("project_id", out var projectIdElement)
            ? projectIdElement.GetString() ?? "" : "";
        _currentProactiveOpportunityId = candidate.TryGetProperty("opportunity_id", out var opportunityIdElement)
            ? opportunityIdElement.GetString() ?? "" : "";
        _currentProactiveKind = candidate.TryGetProperty("kind", out var kindElement)
            ? kindElement.GetString() ?? "" : "";
        ProactiveReplyButton.Content = _currentProactiveKind is "suggestion" or "digest" ? "查看依据" : "回应";
        var title = candidate.TryGetProperty("title", out var titleElement)
            ? titleElement.GetString() ?? "未名子想说" : "未名子想说";
        var text = candidate.TryGetProperty("message", out var messageElement)
            ? messageElement.GetString() ?? "主人，我来看看你。" : "主人，我来看看你。";
        var reason = candidate.TryGetProperty("reason", out var reasonElement)
            ? reasonElement.GetString() ?? "" : "";
        ProactiveReasonText.Text = string.IsNullOrWhiteSpace(reason) ? "" : $"这次出现是因为：{reason}";
        ProactiveBubbleActions.Visibility = Visibility.Visible;
        BubbleHistoryPanel.Visibility = Visibility.Collapsed;
        RequestExpression(AvatarExpression.Shy, 2.2, true);
        PlayReaction(ReactionCue.Reminder);
        ShowPetBubble(text, false, BubbleKind.Proactive, true);
        AppendChat($"\n\n未名子 · 主动陪伴\n{text}", true);
        _tray.BalloonTipTitle = title;
        _tray.BalloonTipText = text;
        _tray.ShowBalloonTip(8000);
        ShowToast(text, "#F0FAF2");
        if (!string.IsNullOrWhiteSpace(_currentProactiveCandidateId))
            _ = AcknowledgeProactiveDisplayAsync(_currentProactiveCandidateId);
        _ = RefreshProactiveAsync();
    }

    private void ShowProactiveDetails(JsonElement details)
    {
        var text = details.TryGetProperty("text", out var textElement)
            ? textElement.GetString() ?? "没有可显示的依据" : "没有可显示的依据";
        var projectId = details.TryGetProperty("project_id", out var projectElement)
            ? projectElement.GetString() ?? "" : "";
        var opportunityId = details.TryGetProperty("opportunity_id", out var opportunityElement)
            ? opportunityElement.GetString() ?? "" : "";
        ProactiveReasonText.Text = text;
        AppendChat($"\n\n未名子 · 建议依据\n{text}", true);
        SetPetMode(false);
        if (!string.IsNullOrWhiteSpace(projectId))
        {
            _pendingEvidenceProjectId = projectId;
            _pendingEvidenceOpportunityId = opportunityId;
            MainTabs.SelectedIndex = 2;
            _ = RefreshProjectsAsync();
        }
        else
        {
            MainTabs.SelectedIndex = 4;
            _ = RefreshProactiveAsync();
        }
        ShowToast("已经为主人展开这条建议的依据和权限边界。", "#EFF8F1");
    }

    private async Task AcknowledgeProactiveDisplayAsync(string candidateId)
    {
        try
        {
            await _pipe.SendAsync(new
            {
                type = "proactive.delivery_ack",
                request_id = Guid.NewGuid().ToString("N"),
                candidate_id = candidateId,
                status = "displayed",
            });
        }
        catch
        {
            // 未确认的消息会由核心在下次连接时恢复。
        }
    }

    private async Task RefreshRemindersAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new { type = "reminders.list", request_id = Guid.NewGuid().ToString("N") });
        }
        catch (Exception ex)
        {
            TaskFeedbackText.Text = ex.Message;
        }
    }

    private void RenderReminders(JsonElement reminders)
    {
        var items = new List<ReminderViewItem>();
        if (reminders.ValueKind == JsonValueKind.Array)
        {
            foreach (var reminder in reminders.EnumerateArray())
            {
                var id = reminder.TryGetProperty("reminder_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var title = reminder.TryGetProperty("title", out var titleElement) ? titleElement.GetString() ?? "提醒" : "提醒";
                var message = reminder.TryGetProperty("message", out var messageElement) ? messageElement.GetString() ?? "" : "";
                var dueAt = reminder.TryGetProperty("due_at", out var dueElement) ? dueElement.GetDouble() : 0;
                items.Add(new ReminderViewItem
                {
                    Id = id,
                    Title = title,
                    Message = message,
                    DueAt = DateTimeOffset.FromUnixTimeSeconds((long)dueAt).LocalDateTime,
                });
            }
        }
        ReminderListBox.ItemsSource = items;
    }

    private void RenderMemories(JsonElement memories)
    {
        var selectedId = (MemoryListBox.SelectedItem as MemoryViewItem)?.Id;
        var items = new List<MemoryViewItem>();
        if (memories.ValueKind == JsonValueKind.Array)
        {
            foreach (var memory in memories.EnumerateArray())
            {
                var id = memory.TryGetProperty("memory_id", out var idElement) ? idElement.GetString() ?? "" : "";
                var content = memory.TryGetProperty("content", out var contentElement)
                    ? contentElement.GetString() ?? "" : "";
                var pinned = memory.TryGetProperty("pinned", out var pinnedElement) && pinnedElement.GetBoolean();
                if (!string.IsNullOrWhiteSpace(id) && !string.IsNullOrWhiteSpace(content))
                    items.Add(new MemoryViewItem { Id = id, Content = content, Pinned = pinned });
            }
        }
        MemoryListBox.ItemsSource = items;
        MemoryListBox.SelectedItem = items.FirstOrDefault(item => item.Id == selectedId) ?? items.FirstOrDefault();
        MemoryEmptyText.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        MemoryEditBox.IsEnabled = items.Count > 0;
        if (items.Count == 0) MemoryEditBox.Clear();
        MemoryFeedbackText.Text = items.Count == 0 ? "这个分类里暂时没有记忆。" : $"共显示 {items.Count} 条记忆。";
    }

    private void UpdateAvatarState(double energy, bool working)
    {
        AvatarImage.Opacity = Math.Clamp(0.72 + energy * 0.28, 0.72, 1.0);
        PetAvatarImage.Opacity = Math.Clamp(0.78 + energy * 0.22, 0.78, 1.0);
        AvatarBorder.BorderBrush = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(working ? "#9DBB78" : "#5078A98C"));
        PetAvatarBorder.BorderBrush = AvatarBorder.BorderBrush;
        SetWorkingVisual(working);
        if (!working) return;
        var animation = new DoubleAnimation(0.86, 1.0, TimeSpan.FromMilliseconds(650))
        {
            AutoReverse = true,
            RepeatBehavior = new RepeatBehavior(2),
        };
        AvatarImage.BeginAnimation(OpacityProperty, animation);
    }

    private void StartPresenceAnimations()
    {
        var breathe = new DoubleAnimation(0.988, 1.012, TimeSpan.FromSeconds(2.8))
        {
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        AvatarScale.BeginAnimation(ScaleTransform.ScaleXProperty, breathe);
        AvatarScale.BeginAnimation(ScaleTransform.ScaleYProperty, breathe);
        PetAvatarScale.BeginAnimation(ScaleTransform.ScaleXProperty, breathe);
        PetAvatarScale.BeginAnimation(ScaleTransform.ScaleYProperty, breathe);

        var floating = new DoubleAnimation(-2.2, 2.2, TimeSpan.FromSeconds(3.4))
        {
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        AvatarFloat.BeginAnimation(TranslateTransform.YProperty, floating);
        PetAvatarFloat.BeginAnimation(TranslateTransform.YProperty, floating);
    }

    private void ScheduleNextIdleGesture()
    {
        _idleGestureTimer.Interval = TimeSpan.FromMilliseconds(_animationRandom.Next(7200, 14800));
    }

    private static DoubleAnimationUsingKeyFrames ReactionFrames(
        int durationMilliseconds,
        params (double progress, double value)[] values)
    {
        var animation = new DoubleAnimationUsingKeyFrames
        {
            Duration = TimeSpan.FromMilliseconds(durationMilliseconds),
            FillBehavior = FillBehavior.HoldEnd,
        };
        foreach (var (progress, value) in values)
        {
            animation.KeyFrames.Add(new EasingDoubleKeyFrame(
                value,
                KeyTime.FromPercent(Math.Clamp(progress, 0, 1)),
                new QuadraticEase { EasingMode = EasingMode.EaseInOut }));
        }
        return animation;
    }

    private static void AnimatePair(
        Animatable first,
        Animatable second,
        DependencyProperty property,
        DoubleAnimationUsingKeyFrames animation)
    {
        var secondAnimation = animation.Clone();
        first.BeginAnimation(property, animation, HandoffBehavior.SnapshotAndReplace);
        second.BeginAnimation(property, secondAnimation, HandoffBehavior.SnapshotAndReplace);
    }

    private void ClearReactionAnimations()
    {
        foreach (var transform in new[] { AvatarReactionOffset, PetAvatarReactionOffset })
        {
            transform.BeginAnimation(TranslateTransform.XProperty, null);
            transform.BeginAnimation(TranslateTransform.YProperty, null);
            transform.X = 0;
            transform.Y = 0;
        }
        foreach (var transform in new[] { AvatarReactionScale, PetAvatarReactionScale })
        {
            transform.BeginAnimation(ScaleTransform.ScaleXProperty, null);
            transform.BeginAnimation(ScaleTransform.ScaleYProperty, null);
            transform.ScaleX = 1;
            transform.ScaleY = 1;
        }
        foreach (var transform in new[] { AvatarTilt, PetAvatarTilt })
        {
            transform.BeginAnimation(RotateTransform.AngleProperty, null);
            transform.Angle = 0;
        }
        PetShadowScale.BeginAnimation(ScaleTransform.ScaleXProperty, null);
        PetShadowScale.ScaleX = 1;
    }

    private async void ShowReactionEmote(string text, string color, int holdMilliseconds, int priority)
    {
        if (DateTimeOffset.UtcNow < _emoteHoldUntil && priority < _emotePriority) return;
        var generation = ++_emoteGeneration;
        _emotePriority = priority;
        _emoteHoldUntil = DateTimeOffset.UtcNow.AddMilliseconds(holdMilliseconds);
        AvatarEmote.Text = text;
        PetEmote.Text = text;
        var brush = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(color));
        AvatarEmote.Foreground = brush;
        PetEmote.Foreground = brush;
        AvatarEmoteBadge.BorderBrush = new SolidColorBrush(brush.Color) { Opacity = 0.35 };
        PetEmoteBadge.BorderBrush = AvatarEmoteBadge.BorderBrush;
        AvatarEmoteBadge.Visibility = Visibility.Visible;
        PetEmoteBadge.Visibility = Visibility.Visible;
        AvatarEmoteBadge.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(150)));
        PetEmoteBadge.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(150)));
        var pop = new DoubleAnimation(0.68, 1.08, TimeSpan.FromMilliseconds(210))
        {
            EasingFunction = new BackEase { Amplitude = 0.35, EasingMode = EasingMode.EaseOut },
        };
        AvatarEmoteScale.BeginAnimation(ScaleTransform.ScaleXProperty, pop);
        AvatarEmoteScale.BeginAnimation(ScaleTransform.ScaleYProperty, pop);
        PetEmoteScale.BeginAnimation(ScaleTransform.ScaleXProperty, pop);
        PetEmoteScale.BeginAnimation(ScaleTransform.ScaleYProperty, pop);
        await Task.Delay(Math.Max(450, holdMilliseconds));
        if (_exitRequested || generation != _emoteGeneration) return;
        var fade = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(220));
        fade.Completed += (_, _) =>
        {
            if (generation != _emoteGeneration) return;
            AvatarEmoteBadge.Visibility = Visibility.Collapsed;
            PetEmoteBadge.Visibility = Visibility.Collapsed;
            _emotePriority = 0;
            _emoteHoldUntil = DateTimeOffset.MinValue;
        };
        AvatarEmoteBadge.BeginAnimation(OpacityProperty, fade);
        PetEmoteBadge.BeginAnimation(OpacityProperty, fade.Clone());
    }

    private async void PlayReaction(ReactionCue cue, bool showEmote = true)
    {
        var generation = ++_reactionGeneration;
        ClearReactionAnimations();
        var duration = cue switch
        {
            ReactionCue.Success or ReactionCue.Welcome => 920,
            ReactionCue.Error => 680,
            ReactionCue.Headpat => 980,
            ReactionCue.Reminder => 900,
            ReactionCue.Tool or ReactionCue.Thinking or ReactionCue.Working => 1050,
            ReactionCue.Idle => 1250,
            _ => 720,
        };
        var (emote, color) = cue switch
        {
            ReactionCue.Success => ("✓", "#68A77E"),
            ReactionCue.Error => ("!", "#D57E72"),
            ReactionCue.Headpat => ("♡", "#D7829B"),
            ReactionCue.Reminder => ("!", "#D4A54D"),
            ReactionCue.Permission => ("?", "#D4A54D"),
            ReactionCue.Tool or ReactionCue.Working => ("✦", "#8C80C2"),
            ReactionCue.Thinking => ("…", "#879BC4"),
            ReactionCue.Welcome => ("♪", "#6FA788"),
            ReactionCue.Idle => ("♪", "#87A895"),
            _ => ("", "#78A98C"),
        };

        switch (cue)
        {
            case ReactionCue.Listening:
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.YProperty,
                    ReactionFrames(duration, (0, 0), (0.42, 5), (1, 0)));
                break;
            case ReactionCue.Thinking:
            case ReactionCue.Tool:
            case ReactionCue.Working:
                AnimatePair(AvatarTilt, PetAvatarTilt, RotateTransform.AngleProperty,
                    ReactionFrames(duration, (0, 0), (0.28, -2.8), (0.63, 2.1), (1, 0)));
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.YProperty,
                    ReactionFrames(duration, (0, 0), (0.45, -3), (1, 0)));
                break;
            case ReactionCue.Speaking:
                AnimatePair(AvatarReactionScale, PetAvatarReactionScale, ScaleTransform.ScaleYProperty,
                    ReactionFrames(duration, (0, 1), (0.32, 0.985), (0.68, 1.018), (1, 1)));
                break;
            case ReactionCue.Success:
            case ReactionCue.Welcome:
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.YProperty,
                    ReactionFrames(duration, (0, 0), (0.28, -15), (0.53, 0), (0.72, -6), (1, 0)));
                PetShadowScale.BeginAnimation(ScaleTransform.ScaleXProperty,
                    ReactionFrames(duration, (0, 1), (0.28, 0.78), (0.53, 1), (0.72, 0.9), (1, 1)));
                break;
            case ReactionCue.Error:
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.XProperty,
                    ReactionFrames(duration, (0, 0), (0.18, -7), (0.36, 6), (0.54, -5), (0.72, 4), (1, 0)));
                break;
            case ReactionCue.Headpat:
                AnimatePair(AvatarReactionScale, PetAvatarReactionScale, ScaleTransform.ScaleXProperty,
                    ReactionFrames(duration, (0, 1), (0.28, 1.055), (0.58, 1.025), (1, 1)));
                AnimatePair(AvatarReactionScale, PetAvatarReactionScale, ScaleTransform.ScaleYProperty,
                    ReactionFrames(duration, (0, 1), (0.28, 0.91), (0.58, 0.975), (1, 1)));
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.YProperty,
                    ReactionFrames(duration, (0, 0), (0.28, 7), (0.58, 2), (1, 0)));
                break;
            case ReactionCue.Reminder:
            case ReactionCue.Permission:
                AnimatePair(AvatarReactionScale, PetAvatarReactionScale, ScaleTransform.ScaleXProperty,
                    ReactionFrames(duration, (0, 1), (0.3, 1.07), (0.58, 0.98), (1, 1)));
                AnimatePair(AvatarReactionScale, PetAvatarReactionScale, ScaleTransform.ScaleYProperty,
                    ReactionFrames(duration, (0, 1), (0.3, 1.07), (0.58, 0.98), (1, 1)));
                break;
            case ReactionCue.Idle:
                AnimatePair(AvatarTilt, PetAvatarTilt, RotateTransform.AngleProperty,
                    ReactionFrames(duration, (0, 0), (0.32, -1.8), (0.68, 1.2), (1, 0)));
                AnimatePair(AvatarReactionOffset, PetAvatarReactionOffset, TranslateTransform.XProperty,
                    ReactionFrames(duration, (0, 0), (0.48, 2), (1, 0)));
                break;
        }

        var emotePriority = cue switch
        {
            ReactionCue.Error => 4,
            ReactionCue.Headpat or ReactionCue.Reminder or ReactionCue.Permission => 3,
            ReactionCue.Success or ReactionCue.Tool or ReactionCue.Working or ReactionCue.Thinking => 2,
            ReactionCue.Welcome => 1,
            _ => 0,
        };
        if (showEmote && !string.IsNullOrEmpty(emote))
            ShowReactionEmote(emote, color, duration + 480, emotePriority);
        await Task.Delay(duration + 80);
        if (_exitRequested || generation != _reactionGeneration) return;
        ClearReactionAnimations();
    }

    private void SetWorkingVisual(bool working)
    {
        BusyIndicator.Visibility = working ? Visibility.Visible : Visibility.Collapsed;
        ActivityRing.Visibility = working ? Visibility.Visible : Visibility.Collapsed;
        PetBusyRing.Visibility = working ? Visibility.Visible : Visibility.Collapsed;
        var rotation = working
            ? new DoubleAnimation(0, 360, TimeSpan.FromSeconds(4.2)) { RepeatBehavior = RepeatBehavior.Forever }
            : null;
        ActivityRingRotation.BeginAnimation(RotateTransform.AngleProperty, rotation);
        PetBusyRingRotation.BeginAnimation(RotateTransform.AngleProperty, rotation);
    }

    private void ApplyMoodVisual(string mood)
    {
        var color = mood.Contains("开心") || mood.Contains("高兴") || mood.Contains("期待")
            ? "#70A98A"
            : mood.Contains("不安") || mood.Contains("紧张") || mood.Contains("难过")
                ? "#D69A8B"
                : mood.Contains("困") || mood.Contains("疲")
                    ? "#9B9AC2"
                    : "#78A98C";
        var accent = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(color));
        EnergyBar.Foreground = accent;
        AvatarGlow.Fill = new SolidColorBrush(accent.Color) { Opacity = 0.18 };
        PetAura.Fill = new SolidColorBrush(accent.Color) { Opacity = 0.18 };
        MoodPill.Background = new SolidColorBrush(accent.Color) { Opacity = 0.18 };
    }

    private static string CompactBubbleText(string text)
    {
        var clean = text.Replace("\r", "").Trim();
        if (string.IsNullOrWhiteSpace(clean)) return "未名子在这里陪着主人喵。";
        return clean.Length > 168 ? clean[..168] + "…" : clean;
    }

    private static int FindSentenceBoundary(StringBuilder buffer)
    {
        const string endings = "。！？!?；;\n";
        for (var index = 0; index < buffer.Length; index++)
        {
            if (endings.Contains(buffer[index])) return index;
        }
        return -1;
    }

    private void FeedBubbleToken(string token)
    {
        _bubbleSentenceBuffer.Append(token);
        while (true)
        {
            var boundary = FindSentenceBoundary(_bubbleSentenceBuffer);
            if (boundary < 0) break;
            var sentence = _bubbleSentenceBuffer.ToString(0, boundary + 1).Trim();
            _bubbleSentenceBuffer.Remove(0, boundary + 1);
            if (!string.IsNullOrWhiteSpace(sentence))
                ShowPetBubble(sentence, false, BubbleKind.Speech);
        }
    }

    private void FlushBubbleSentence()
    {
        var remainder = _bubbleSentenceBuffer.ToString().Trim();
        _bubbleSentenceBuffer.Clear();
        if (!string.IsNullOrWhiteSpace(remainder))
            ShowPetBubble(remainder, false, BubbleKind.Speech);
    }

    private static TimeSpan BubbleLifetime(string text)
    {
        var seconds = Math.Clamp(4.5 + text.Length * 0.075, 5.0, 18.0);
        return TimeSpan.FromSeconds(seconds);
    }

    private void RememberBubble(string text, BubbleKind kind)
    {
        var clean = CompactBubbleText(text);
        if (_bubbleHistory.Count > 0 && _bubbleHistory[^1].Text == clean && _bubbleHistory[^1].Kind == kind)
        {
            _bubbleHistoryIndex = _bubbleHistory.Count - 1;
            RefreshBubbleHistoryControls();
            return;
        }
        _bubbleHistory.Add(new BubbleEntry(clean, kind, DateTimeOffset.Now));
        while (_bubbleHistory.Count > 3) _bubbleHistory.RemoveAt(0);
        _bubbleHistoryIndex = _bubbleHistory.Count - 1;
        RefreshBubbleHistoryControls();
    }

    private void RefreshBubbleHistoryControls()
    {
        BubbleHistoryPanel.Visibility = ProactiveBubbleActions.Visibility == Visibility.Visible
            ? Visibility.Collapsed
            : _bubbleHistory.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
        BubbleHistoryPositionText.Text = _bubbleHistory.Count == 0
            ? "0/0"
            : $"{Math.Clamp(_bubbleHistoryIndex + 1, 1, _bubbleHistory.Count)}/{_bubbleHistory.Count}";
    }

    private void ApplyBubbleKind(BubbleKind kind)
    {
        _currentBubbleKind = kind;
        var (label, background, border, foreground) = kind switch
        {
            BubbleKind.Thinking => ("正在思考", "#F2F6FF", "#879BC4", "#435473"),
            BubbleKind.Tool => ("正在查资料", "#F3F0FF", "#9B8CC8", "#50466F"),
            BubbleKind.Permission => ("等待主人确认", "#FFF8E8", "#D5A654", "#705326"),
            BubbleKind.Reminder => ("未名子 · 提醒", "#F0FAF2", "#73A886", "#385D45"),
            BubbleKind.Proactive => ("未名子 · 主动陪伴", "#F0FAF2", "#73A886", "#385D45"),
            BubbleKind.Error => ("连接或任务异常", "#FFF0EC", "#CF887B", "#743F37"),
            _ => ("未名子", "#F8FFFC", "#78A98C", "#35423A"),
        };
        BubbleKindText.Text = label;
        BubbleKindText.Foreground = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(foreground));
        PetBubbleText.Foreground = BubbleKindText.Foreground;
        PetBubbleBorder.Background = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(background));
        PetBubbleBorder.BorderBrush = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(border));
    }

    private void RenderBubbleText(string text)
    {
        var clean = CompactBubbleText(text);
        PetBubbleText.Inlines.Clear();
        var pattern = new Regex(@"(`[^`\r\n]+`|\[[^\]\r\n]+\]\(https?://[^\s)]+\))");
        var cursor = 0;
        foreach (Match match in pattern.Matches(clean))
        {
            if (match.Index > cursor) PetBubbleText.Inlines.Add(new Run(clean[cursor..match.Index]));
            var token = match.Value;
            if (token.StartsWith('`') && token.EndsWith('`'))
            {
                PetBubbleText.Inlines.Add(new Run(token[1..^1])
                {
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    Background = new SolidColorBrush(System.Windows.Media.Color.FromArgb(28, 70, 90, 78)),
                });
            }
            else
            {
                var split = token.LastIndexOf("](", StringComparison.Ordinal);
                var label = token[1..split];
                var target = token[(split + 2)..^1];
                if (Uri.TryCreate(target, UriKind.Absolute, out var uri) &&
                    (uri.Scheme == Uri.UriSchemeHttps || uri.Scheme == Uri.UriSchemeHttp))
                {
                    var link = new Hyperlink(new Run(label)) { NavigateUri = uri, ToolTip = target };
                    link.Click += (_, _) => Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
                    PetBubbleText.Inlines.Add(link);
                }
                else PetBubbleText.Inlines.Add(new Run(label));
            }
            cursor = match.Index + match.Length;
        }
        if (cursor < clean.Length) PetBubbleText.Inlines.Add(new Run(clean[cursor..]));
    }

    private void ShowPetBubble(
        string text,
        bool autoHide,
        BubbleKind kind = BubbleKind.Speech,
        bool remember = false)
    {
        var clean = CompactBubbleText(text);
        if (kind != BubbleKind.Proactive)
        {
            ProactiveBubbleActions.Visibility = Visibility.Collapsed;
            RefreshBubbleHistoryControls();
        }
        _lastBubbleSegment = clean;
        ApplyBubbleKind(kind);
        RenderBubbleText(clean);
        if (remember) RememberBubble(clean, kind);
        PetBubbleBorder.Visibility = Visibility.Visible;
        PetBubbleBorder.BeginAnimation(OpacityProperty,
            new DoubleAnimation(PetBubbleBorder.Opacity, 1, TimeSpan.FromMilliseconds(180)));
        PetBubbleScale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(0.95, 1, TimeSpan.FromMilliseconds(210))
            { EasingFunction = new BackEase { Amplitude = 0.18, EasingMode = EasingMode.EaseOut } });
        PetBubbleScale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(0.95, 1, TimeSpan.FromMilliseconds(210))
            { EasingFunction = new BackEase { Amplitude = 0.18, EasingMode = EasingMode.EaseOut } });
        PetBubbleLift.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(7, 0, TimeSpan.FromMilliseconds(210))
            { EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut } });
        _bubbleTimer.Stop();
        _bubbleAutoHidePending = autoHide;
        if (autoHide)
        {
            _bubbleTimer.Interval = BubbleLifetime(clean);
            _bubbleTimer.Start();
        }
    }

    private void HidePetBubble()
    {
        _bubbleTimer.Stop();
        _bubbleAutoHidePending = false;
        var fade = new DoubleAnimation(PetBubbleBorder.Opacity, 0, TimeSpan.FromMilliseconds(220));
        fade.Completed += (_, _) => PetBubbleBorder.Visibility = Visibility.Hidden;
        PetBubbleBorder.BeginAnimation(OpacityProperty, fade);
        PetBubbleScale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(1, 0.98, TimeSpan.FromMilliseconds(220)));
        PetBubbleScale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(1, 0.98, TimeSpan.FromMilliseconds(220)));
        PetBubbleLift.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(0, 5, TimeSpan.FromMilliseconds(220)));
    }

    private void PetBubbleBorder_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (_bubbleAutoHidePending) _bubbleTimer.Stop();
    }

    private void PetBubbleBorder_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (!_bubbleAutoHidePending) return;
        _bubbleTimer.Interval = TimeSpan.FromSeconds(4);
        _bubbleTimer.Start();
    }

    private void ShowBubbleHistory(int delta)
    {
        if (_bubbleHistory.Count == 0) return;
        _bubbleHistoryIndex = (_bubbleHistoryIndex + delta + _bubbleHistory.Count) % _bubbleHistory.Count;
        var entry = _bubbleHistory[_bubbleHistoryIndex];
        ShowPetBubble(entry.Text, false, entry.Kind);
        RefreshBubbleHistoryControls();
    }

    private void BubblePreviousButton_Click(object sender, RoutedEventArgs e) => ShowBubbleHistory(-1);

    private void BubbleNextButton_Click(object sender, RoutedEventArgs e) => ShowBubbleHistory(1);

    private void ShowToast(string text, string background = "#F3FFF9")
    {
        ToastText.Text = CompactBubbleText(text);
        ToastBorder.Background = new SolidColorBrush((System.Windows.Media.Color)
            System.Windows.Media.ColorConverter.ConvertFromString(background));
        ToastBorder.Visibility = Visibility.Visible;
        ToastBorder.BeginAnimation(OpacityProperty,
            new DoubleAnimation(ToastBorder.Opacity, 1, TimeSpan.FromMilliseconds(160)));
        _toastTimer.Stop();
        _toastTimer.Start();
    }

    private void HideToast()
    {
        _toastTimer.Stop();
        var fade = new DoubleAnimation(ToastBorder.Opacity, 0, TimeSpan.FromMilliseconds(180));
        fade.Completed += (_, _) => ToastBorder.Visibility = Visibility.Collapsed;
        ToastBorder.BeginAnimation(OpacityProperty, fade);
    }

    private void RenderTasks(JsonElement tasks)
    {
        var selectedId = (TaskListBox.SelectedItem as TaskViewItem)?.Id;
        if (tasks.ValueKind != JsonValueKind.Array || tasks.GetArrayLength() == 0)
        {
            TaskListBox.ItemsSource = Array.Empty<TaskViewItem>();
            return;
        }
        var items = new List<TaskViewItem>();
        foreach (var task in tasks.EnumerateArray().Take(8))
        {
            var taskId = task.TryGetProperty("task_id", out var idElement)
                ? idElement.GetString() ?? "" : "";
            var title = task.TryGetProperty("title", out var titleElement)
                ? titleElement.GetString() ?? "未命名任务" : "未命名任务";
            var rawStatus = task.TryGetProperty("status", out var statusElement)
                ? statusElement.GetString() ?? "" : "";
            var status = StatusLabel(rawStatus);
            var completedSteps = 0;
            var totalSteps = 0;
            var resultPath = string.Empty;
            if (task.TryGetProperty("steps", out var steps) && steps.ValueKind == JsonValueKind.Array)
            {
                foreach (var step in steps.EnumerateArray())
                {
                    totalSteps++;
                    if (step.TryGetProperty("status", out var stepStatus) && stepStatus.GetString() == "completed")
                        completedSteps++;
                    if (step.TryGetProperty("output", out var output) &&
                        output.TryGetProperty("absolute_path", out var absolutePath))
                    {
                        resultPath = absolutePath.GetString() ?? resultPath;
                    }
                }
            }
            items.Add(new TaskViewItem
            {
                Id = taskId,
                Status = rawStatus,
                Title = title,
                StatusLabel = status,
                ProgressText = $"步骤 {completedSteps}/{Math.Max(1, totalSteps)}",
                ProgressPercent = totalSteps == 0 ? 0 : completedSteps * 100.0 / totalSteps,
                StatusBrush = StatusBrush(rawStatus),
                ResultPath = resultPath,
                Display = $"{title}　[{status}]\n步骤 {completedSteps}/{Math.Max(1, totalSteps)}",
            });
        }
        TaskListBox.ItemsSource = items;
        TaskListBox.SelectedItem = items.FirstOrDefault(item => item.Id == selectedId) ?? items.FirstOrDefault();
    }

    private void RestoreDraftPlan(JsonElement tasks)
    {
        if (tasks.ValueKind != JsonValueKind.Array) return;
        foreach (var task in tasks.EnumerateArray())
        {
            var status = task.TryGetProperty("status", out var statusElement)
                ? statusElement.GetString() : null;
            if (status == "draft")
            {
                ShowPlanPreview(task, null);
                return;
            }
        }
        if (!string.IsNullOrWhiteSpace(_currentPlanTaskId)) HidePlanPreview();
    }

    private void ShowPlanPreview(JsonElement task, string? source)
    {
        if (!task.TryGetProperty("task_id", out var idElement)) return;
        _currentPlanTaskId = idElement.GetString();
        PlanTitleText.Text = task.TryGetProperty("title", out var titleElement)
            ? titleElement.GetString() ?? "未命名计划" : "未命名计划";
        PlanEditTitleBox.Text = PlanTitleText.Text;
        PlanEditPathBox.Text = string.Empty;
        PlanEditContentBox.Text = string.Empty;
        PlanPptTitleBox.Text = PlanTitleText.Text;
        PlanPptPathBox.Text = string.Empty;
        PlanPptOutlineBox.Text = string.Empty;
        PlanPptVisualSummaryText.Text = string.Empty;
        SelectComboItemByTag(PlanPptBrandTemplateBox, "codex_grid");
        SelectComboItemByTag(PlanPptTemplateBox, "auto_grid");
        _currentPlanIsPresentation = false;
        var lines = new List<string>();
        if (task.TryGetProperty("steps", out var steps) && steps.ValueKind == JsonValueKind.Array)
        {
            var index = 1;
            foreach (var step in steps.EnumerateArray())
            {
                var kind = step.TryGetProperty("kind", out var kindElement)
                    ? kindElement.GetString() ?? "" : "";
                var input = step.TryGetProperty("input", out var inputElement)
                    ? inputElement : default;
                var stepTitle = input.ValueKind == JsonValueKind.Object &&
                    input.TryGetProperty("title", out var stepTitleElement)
                    ? stepTitleElement.GetString() ?? "任务步骤" : "任务步骤";
                var action = kind switch
                {
                    "presentation.image_search" => "检索可追溯配图并暂存（自动步骤）",
                    "presentation.prepare" => "生成临时 PPTX、逐页 PNG 并检查版式（自动步骤）",
                    "workspace.write_presentation" => "主人查看逐页预览后保存 PPTX（需另行许可）",
                    "workspace.write_text" => "创建文本文件（需另行许可）",
                    "workspace.update_text" => "修改文本并备份旧版本（需另行许可）",
                    "workspace.append_text" => "追加文本并备份旧版本（需另行许可）",
                    "workspace.create_directory" => "创建目录（需另行许可）",
                    "workspace.rename" => "重命名（需另行许可）",
                    "web.research" => "只读联网检索（自动步骤）",
                    "document.compose" => "根据来源整理文档（自动步骤）",
                    _ => "整理内容（无外部写入）",
                };
                var line = $"{index}. {stepTitle}\n   {action}";
                if (kind == "presentation.image_search" && input.ValueKind == JsonValueKind.Object &&
                    input.TryGetProperty("queries", out var searchQueries) && searchQueries.ValueKind == JsonValueKind.Array)
                {
                    line += $"\n   配图需求：{searchQueries.GetArrayLength()} 项；图片来源会写入演讲者备注。";
                }
                if (kind == "presentation.prepare" && input.ValueKind == JsonValueKind.Object)
                {
                    _currentPlanIsPresentation = true;
                    if (input.TryGetProperty("deck_title", out var deckTitleElement))
                        PlanPptTitleBox.Text = deckTitleElement.GetString() ?? PlanTitleText.Text;
                    if (input.TryGetProperty("brand_template", out var brandTemplateElement))
                        SelectComboItemByTag(PlanPptBrandTemplateBox, brandTemplateElement.GetString() ?? "codex_grid");
                    if (input.TryGetProperty("layout_strategy", out var layoutElement))
                        SelectComboItemByTag(PlanPptTemplateBox, layoutElement.GetString() ?? "auto_grid");
                    else if (input.TryGetProperty("template", out var templateElement))
                        SelectComboItemByTag(PlanPptTemplateBox, templateElement.GetString() ?? "auto_grid");
                    if (input.TryGetProperty("slides", out var slideOutline) && slideOutline.ValueKind == JsonValueKind.Array)
                    {
                        var outlineLines = new List<string>();
                        var visualDetails = new List<string>();
                        var imageCount = 0;
                        var chartCount = 0;
                        var slideNumber = 1;
                        foreach (var slide in slideOutline.EnumerateArray())
                        {
                            var slideTitle = slide.TryGetProperty("title", out var slideTitleElement)
                                ? slideTitleElement.GetString() ?? $"第 {slideNumber} 页" : $"第 {slideNumber} 页";
                            outlineLines.Add($"{slideNumber}. {slideTitle}");
                            if (slide.TryGetProperty("bullets", out var bullets) && bullets.ValueKind == JsonValueKind.Array)
                            {
                                foreach (var bullet in bullets.EnumerateArray())
                                    outlineLines.Add($"- {bullet.GetString() ?? string.Empty}");
                            }
                            if (slide.TryGetProperty("image_query", out var imageQueryElement))
                            {
                                var imageQuery = imageQueryElement.GetString() ?? string.Empty;
                                if (!string.IsNullOrWhiteSpace(imageQuery))
                                {
                                    outlineLines.Add($"[配图：{imageQuery}]");
                                    imageCount++;
                                }
                            }
                            if (slide.TryGetProperty("chart", out var chartElement) && chartElement.ValueKind == JsonValueKind.Object)
                            {
                                chartCount++;
                                var chartTitle = chartElement.TryGetProperty("title", out var chartTitleElement)
                                    ? chartTitleElement.GetString() ?? "未命名图表" : "未命名图表";
                                var chartType = chartElement.TryGetProperty("type", out var chartTypeElement)
                                    ? chartTypeElement.GetString() ?? "chart" : "chart";
                                visualDetails.Add($"第 {slideNumber} 页：{chartTitle}（{chartType}）");
                            }
                            slideNumber++;
                        }
                        PlanPptOutlineBox.Text = string.Join(Environment.NewLine, outlineLines);
                        PlanPptVisualSummaryText.Text = $"视觉内容：{imageCount} 页配图 · {chartCount} 页原生图表";
                        if (visualDetails.Count > 0)
                            PlanPptVisualSummaryText.Text += Environment.NewLine + string.Join(Environment.NewLine, visualDetails);
                        line += $"\n   大纲：{slideOutline.GetArrayLength()} 张内容页 · {imageCount} 页配图 · {chartCount} 页图表；生成时另加封面与结束页。";
                    }
                }
                if (input.ValueKind == JsonValueKind.Object &&
                    input.TryGetProperty("relative_path", out var pathElement))
                {
                    line += $"\n   位置：{pathElement.GetString()}";
                    if (kind == "workspace.write_presentation")
                        PlanPptPathBox.Text = pathElement.GetString() ?? string.Empty;
                    if (string.IsNullOrWhiteSpace(PlanEditPathBox.Text))
                        PlanEditPathBox.Text = pathElement.GetString() ?? string.Empty;
                }
                if (input.ValueKind == JsonValueKind.Object &&
                    input.TryGetProperty("content", out var contentElement))
                {
                    var content = contentElement.GetString() ?? string.Empty;
                    if (!string.IsNullOrWhiteSpace(content))
                    {
                        var preview = content.Length > 1200 ? content[..1200] + "…" : content;
                        line += $"\n   内容预览：\n{preview}";
                        if (kind == "workspace.write_text" && string.IsNullOrWhiteSpace(PlanEditContentBox.Text))
                            PlanEditContentBox.Text = content;
                    }
                    else if (kind == "workspace.write_text")
                    {
                        line += "\n   内容将在研究步骤完成后生成，并在写入许可卡中再次预览。";
                    }
                }
                lines.Add(line);
                index++;
            }
        }
        PlanDocumentEditExpander.Visibility = _currentPlanIsPresentation ? Visibility.Collapsed : Visibility.Visible;
        PlanPresentationEditExpander.Visibility = _currentPlanIsPresentation ? Visibility.Visible : Visibility.Collapsed;
        var sourceLabel = source switch
        {
            "local_fallback" => "本地保守规划",
            "owner_edited" => "主人修订",
            _ => "模型规划",
        };
        PlanStepsText.Text = $"来源：{sourceLabel}\n\n" + string.Join("\n\n", lines);
        PlanPreviewCard.Visibility = Visibility.Visible;
        TaskFeedbackText.Text = _currentPlanIsPresentation
            ? "请检查 PPT 大纲、模板和保存位置；确认计划后才会生成临时预览，最终保存仍会再次询问。"
            : "请检查步骤、文件位置和内容；尚未执行任何写入。";
    }

    private void HidePlanPreview()
    {
        _currentPlanTaskId = null;
        _currentPlanIsPresentation = false;
        PlanPreviewCard.Visibility = Visibility.Collapsed;
    }

    private static void SelectComboItemByTag(System.Windows.Controls.ComboBox comboBox, string tag)
    {
        foreach (var item in comboBox.Items.OfType<System.Windows.Controls.ComboBoxItem>())
        {
            if (string.Equals(item.Tag?.ToString(), tag, StringComparison.OrdinalIgnoreCase))
            {
                comboBox.SelectedItem = item;
                return;
            }
        }
        comboBox.SelectedIndex = 0;
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
        ApprovalPreviewText.Text = string.Empty;
        _presentationPreviewFiles.Clear();
        _presentationPreviewIndex = 0;
        PresentationPreviewImage.Source = null;
        PresentationApprovalPreview.Visibility = Visibility.Collapsed;
        if (approval.TryGetProperty("step_input", out var stepInput) &&
            stepInput.TryGetProperty("content", out var approvalContent))
        {
            var content = approvalContent.GetString() ?? string.Empty;
            var preview = content.Length > 1600 ? content[..1600] + "…" : content;
            ApprovalPreviewText.Text = string.IsNullOrWhiteSpace(preview) ? "" : $"写入内容预览：\n{preview}";
        }
        if (approval.TryGetProperty("step_input", out var diffInput) &&
            diffInput.TryGetProperty("diff_preview", out var diffElement))
        {
            var diff = diffElement.GetString() ?? string.Empty;
            ApprovalPreviewText.Text = string.IsNullOrWhiteSpace(diff) ? ApprovalPreviewText.Text : $"变更差异：\n{diff}";
        }
        if (approval.TryGetProperty("step_input", out var presentationInput) &&
            presentationInput.ValueKind == JsonValueKind.Object &&
            presentationInput.TryGetProperty("preview_files", out var previewFiles) &&
            previewFiles.ValueKind == JsonValueKind.Array)
        {
            foreach (var file in previewFiles.EnumerateArray())
            {
                var path = file.GetString() ?? string.Empty;
                if (File.Exists(path)) _presentationPreviewFiles.Add(path);
            }
            if (_presentationPreviewFiles.Count > 0)
            {
                var deckTitle = presentationInput.TryGetProperty("deck_title", out var deckTitleElement)
                    ? deckTitleElement.GetString() ?? "演示文稿" : "演示文稿";
                var brand = presentationInput.TryGetProperty("brand_template", out var brandElement)
                    ? PresentationBrandLabel(brandElement.GetString()) : "Codex Grid";
                var layout = presentationInput.TryGetProperty("layout_strategy", out var layoutElement)
                    ? PresentationTemplateLabel(layoutElement.GetString())
                    : presentationInput.TryGetProperty("template", out var templateElement)
                        ? PresentationTemplateLabel(templateElement.GetString()) : "自动网格";
                var imageCount = presentationInput.TryGetProperty("image_count", out var imageCountElement)
                    ? imageCountElement.GetInt32() : 0;
                var chartCount = presentationInput.TryGetProperty("chart_count", out var chartCountElement)
                    ? chartCountElement.GetInt32() : 0;
                ApprovalPreviewText.Text = $"演示预览：{deckTitle}\n品牌：{brand} · 版式：{layout}\n视觉：{imageCount} 页配图 · {chartCount} 页原生图表\n请逐页检查后再决定是否保存。";
                PresentationApprovalPreview.Visibility = Visibility.Visible;
                RenderPresentationPreviewPage();
            }
        }
        ApprovalCard.Visibility = Visibility.Visible;
    }

    private void HideApproval()
    {
        _currentApprovalId = null;
        ApprovalPreviewText.Text = string.Empty;
        _presentationPreviewFiles.Clear();
        _presentationPreviewIndex = 0;
        PresentationPreviewImage.Source = null;
        PresentationApprovalPreview.Visibility = Visibility.Collapsed;
        ApprovalCard.Visibility = Visibility.Collapsed;
    }

    private static string PresentationTemplateLabel(string? template) => template switch
    {
        "text_brief" => "文字简报",
        "report_flow" => "三段汇报",
        _ => "自动网格",
    };

    private static string PresentationBrandLabel(string? brand) => brand switch
    {
        "unnameko_green" => "未名子柔绿",
        "night_code" => "夜色代码",
        _ => "Codex Grid",
    };

    private void RenderPresentationPreviewPage()
    {
        if (_presentationPreviewFiles.Count == 0) return;
        _presentationPreviewIndex = Math.Clamp(_presentationPreviewIndex, 0, _presentationPreviewFiles.Count - 1);
        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri(_presentationPreviewFiles[_presentationPreviewIndex], UriKind.Absolute);
            bitmap.EndInit();
            bitmap.Freeze();
            PresentationPreviewImage.Source = bitmap;
            PresentationPreviewPageText.Text = $"第 {_presentationPreviewIndex + 1} / {_presentationPreviewFiles.Count} 页";
        }
        catch (Exception ex)
        {
            PresentationPreviewPageText.Text = $"预览加载失败：{ex.Message}";
        }
    }

    private void PreviousPresentationPreviewButton_Click(object sender, RoutedEventArgs e)
    {
        if (_presentationPreviewFiles.Count == 0) return;
        _presentationPreviewIndex = (_presentationPreviewIndex - 1 + _presentationPreviewFiles.Count) % _presentationPreviewFiles.Count;
        RenderPresentationPreviewPage();
    }

    private void NextPresentationPreviewButton_Click(object sender, RoutedEventArgs e)
    {
        if (_presentationPreviewFiles.Count == 0) return;
        _presentationPreviewIndex = (_presentationPreviewIndex + 1) % _presentationPreviewFiles.Count;
        RenderPresentationPreviewPage();
    }

    private void OpenPresentationPreviewButton_Click(object sender, RoutedEventArgs e)
    {
        if (_presentationPreviewFiles.Count == 0) return;
        var directory = Path.GetDirectoryName(_presentationPreviewFiles[0]);
        if (!string.IsNullOrWhiteSpace(directory)) OpenLocalPath(directory);
    }

    private static string StatusLabel(string? status) => status switch
    {
        "draft" => "等待确认计划",
        "waiting_approval" => "等待确认",
        "running" => "执行中",
        "paused" => "已暂停",
        "completed" => "已完成",
        "failed" => "失败",
        "cancelled" => "已取消",
        _ => status ?? "未知",
    };

    private static System.Windows.Media.Brush StatusBrush(string? status)
    {
        var color = status switch
        {
            "completed" => "#CDE8D5",
            "running" => "#DCEBC8",
            "waiting_approval" => "#F6E1B8",
            "draft" => "#DCE6F6",
            "paused" => "#E6DFF2",
            "failed" => "#F4CEC7",
            "cancelled" => "#E4E5E4",
            _ => "#E8EFEA",
        };
        return new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(color));
    }

    private async Task SendCurrentAsync()
    {
        var text = InputBox.Text.Trim();
        if (_busy || _perceptionPreparing) return;
        if (string.IsNullOrWhiteSpace(text))
        {
            if (string.IsNullOrWhiteSpace(_pendingPerceptionContext)) return;
            if (!_perceptionPolicy.SendSummariesToModel)
            {
                PerceptionFeedbackText.Text = "当前设置为仅本地预览；如需她回答，请允许发送筛选后的文字摘要。";
                MainTabs.SelectedIndex = 6;
                return;
            }
            text = "请看看我刚刚授权的桌面内容，告诉我你注意到了什么。";
        }
        if (!_connected)
        {
            AppendChat("\n\n系统\n桌面核心还在连接，请稍等一下。", true);
            return;
        }
        _perceptionPreparing = true;
        InputBox.IsEnabled = false;
        try
        {
            var automaticContext = await BuildAutomaticPerceptionContextAsync(text);
            InputBox.Clear();
            AppendChat($"\n\n主人\n{text}", true);
            var contextParts = new List<string>();
            if (_perceptionPolicy.SendSummariesToModel && !string.IsNullOrWhiteSpace(_pendingPerceptionContext))
                contextParts.Add(_pendingPerceptionContext);
            if (!string.IsNullOrWhiteSpace(automaticContext)) contextParts.Add(automaticContext);
            var deliveredText = contextParts.Count == 0
                ? text
                : $"{text}\n\n【本轮获得授权的桌面上下文】\n{string.Join("\n\n", contextParts)}";
            await _pipe.SendAsync(new
            {
                type = "chat.send",
                request_id = Guid.NewGuid().ToString("N"),
                text = deliveredText,
            });
            if (_perceptionPolicy.SendSummariesToModel) ClearPerceptionContext();
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
        finally
        {
            _perceptionPreparing = false;
        }
        ChatScroller.ScrollToEnd();
    }

    private async Task<string> BuildAutomaticPerceptionContextAsync(string text)
    {
        if (_perceptionPolicy.Mode == PerceptionMode.Privacy || _lastExternalWindow == nint.Zero) return string.Empty;
        var processName = _currentExternalProcess;
        if (_perceptionPolicy.Paused)
        {
            return _perceptionPolicy.WantsWindowContext(text)
                ? "【感知状态】桌面感知当前由主人暂停，因此没有读取窗口内容。" : string.Empty;
        }
        if (_perceptionPolicy.IsBlocked(processName))
        {
            return _perceptionPolicy.WantsWindowContext(text)
                ? $"【感知状态】{processName} 位于禁止名单，因此没有读取窗口内容。" : string.Empty;
        }

        var trusted = _perceptionPolicy.IsTrusted(processName);
        var metadata = $"【当前活动应用】{(string.IsNullOrWhiteSpace(processName) ? "未知应用" : processName)}";
        if (trusted && !string.IsNullOrWhiteSpace(_currentExternalTitle)) metadata += $" · {_currentExternalTitle}";
        if (!_perceptionPolicy.WantsWindowContext(text))
            return _perceptionPolicy.SendSummariesToModel ? metadata : string.Empty;
        if (!_perceptionPolicy.CanUseNaturalLanguage(processName, text))
            return _perceptionPolicy.SendSummariesToModel ? metadata : string.Empty;

        PerceptionFeedbackText.Text = "主人已经用自然语言授权，正在读取当前窗口…";
        RequestExpression(AvatarExpression.Focused, 2.0, true);
        SetWorkingVisual(true);
        var snapshot = await Task.Run(() => _perception.CaptureStructure(_lastExternalWindow));
        var parts = new List<string> { metadata, snapshot.ToContext(40) };
        if (_perceptionPolicy.WantsScreenshot(text) && _connected)
        {
            var visionContext = await RequestDirectScreenshotContextAsync(_lastExternalWindow);
            if (!string.IsNullOrWhiteSpace(visionContext)) parts.Add(visionContext);
        }
        var result = string.Join("\n\n", parts);
        if (!_perceptionPolicy.SendSummariesToModel)
        {
            AddPerceptionContext(result);
            PerceptionFeedbackText.Text = "已在本地完成读取并放入预览；根据当前设置，没有发送给 DeepSeek。";
            return "【感知状态】窗口已在本地读取，但主人设置为不向对话模型发送感知摘要。";
        }
        return result;
    }

    private async Task<string> RequestDirectScreenshotContextAsync(nint handle)
    {
        var path = CaptureWindowToTemporaryFile(handle);
        var requestId = Guid.NewGuid().ToString("N");
        var completion = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        _directPerceptionRequests[requestId] = completion;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "perception.image",
                request_id = requestId,
                path,
                source = "direct",
            });
            return await completion.Task.WaitAsync(TimeSpan.FromSeconds(190));
        }
        catch
        {
            _directPerceptionRequests.Remove(requestId);
            try { File.Delete(path); } catch { }
            throw;
        }
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
        if (e.OriginalSource is DependencyObject source &&
            FindVisualParent<System.Windows.Controls.Button>(source) is not null) return;
        BeginWindowDrag(e);
    }

    private static T? FindVisualParent<T>(DependencyObject? source) where T : DependencyObject
    {
        while (source is not null)
        {
            if (source is T match) return match;
            source = VisualTreeHelper.GetParent(source);
        }
        return null;
    }

    private System.Windows.Point CursorPositionDip()
    {
        var cursor = Forms.Cursor.Position;
        var transform = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
            ?? Matrix.Identity;
        return transform.Transform(new System.Windows.Point(cursor.X, cursor.Y));
    }

    private Rect CurrentWorkingAreaDip()
    {
        var cursor = Forms.Cursor.Position;
        var bounds = Forms.Screen.FromPoint(cursor).WorkingArea;
        var transform = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
            ?? Matrix.Identity;
        var topLeft = transform.Transform(new System.Windows.Point(bounds.Left, bounds.Top));
        var bottomRight = transform.Transform(new System.Windows.Point(bounds.Right, bounds.Bottom));
        return new Rect(topLeft, bottomRight);
    }

    private static double ElasticClamp(double value, double minimum, double maximum)
    {
        if (value < minimum) return minimum + (value - minimum) * 0.2;
        if (value > maximum) return maximum + (value - maximum) * 0.2;
        return value;
    }

    private void BeginWindowDrag(MouseButtonEventArgs e)
    {
        if (e.ChangedButton != MouseButton.Left || e.ButtonState != MouseButtonState.Pressed) return;
        BeginAnimation(LeftProperty, null);
        BeginAnimation(TopProperty, null);
        _isWindowDragging = true;
        _dragStartCursor = CursorPositionDip();
        _lastDragCursor = _dragStartCursor;
        _lastDragSampleAt = DateTimeOffset.UtcNow;
        _dragStartLeft = Left;
        _dragStartTop = Top;
        _dragVelocity = new Vector();
        _dragSnapCandidate = DragSnapEdges.None;
        Mouse.Capture(this, CaptureMode.SubTree);
        SetDragVisual(true);
        e.Handled = true;
    }

    private void Window_DragMouseMove(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (!_isWindowDragging || e.LeftButton != MouseButtonState.Pressed) return;
        var cursor = CursorPositionDip();
        var area = CurrentWorkingAreaDip();
        var width = ActualWidth > 0 ? ActualWidth : Width;
        var height = ActualHeight > 0 ? ActualHeight : Height;
        var desiredLeft = _dragStartLeft + cursor.X - _dragStartCursor.X;
        var desiredTop = _dragStartTop + cursor.Y - _dragStartCursor.Y;
        Left = ElasticClamp(desiredLeft, area.Left, Math.Max(area.Left, area.Right - width));
        Top = ElasticClamp(desiredTop, area.Top, Math.Max(area.Top, area.Bottom - height));

        var now = DateTimeOffset.UtcNow;
        var elapsed = Math.Max(0.008, (now - _lastDragSampleAt).TotalSeconds);
        var instantVelocity = new Vector(
            (cursor.X - _lastDragCursor.X) / elapsed,
            (cursor.Y - _lastDragCursor.Y) / elapsed);
        _dragVelocity = new Vector(
            _dragVelocity.X * 0.62 + instantVelocity.X * 0.38,
            _dragVelocity.Y * 0.62 + instantVelocity.Y * 0.38);
        _lastDragCursor = cursor;
        _lastDragSampleAt = now;
        UpdateDragTilt(_dragVelocity.X);
        UpdateSnapCandidate(area, width, height);
        e.Handled = true;
    }

    private void Window_DragMouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (!_isWindowDragging || e.ChangedButton != MouseButton.Left) return;
        CompleteWindowDrag();
        e.Handled = true;
    }

    private void Window_DragLostMouseCapture(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (_isWindowDragging) CompleteWindowDrag();
    }

    private void SetDragVisual(bool dragging)
    {
        var duration = TimeSpan.FromMilliseconds(dragging ? 130 : 190);
        var mainTarget = dragging ? 1.02 : 1;
        var petTarget = dragging ? 1.04 : 1;
        AvatarDragScale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(AvatarDragScale.ScaleX, mainTarget, duration));
        AvatarDragScale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(AvatarDragScale.ScaleY, mainTarget, duration));
        PetAvatarDragScale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(PetAvatarDragScale.ScaleX, petTarget, duration));
        PetAvatarDragScale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(PetAvatarDragScale.ScaleY, petTarget, duration));
        PetShadowScale.BeginAnimation(ScaleTransform.ScaleXProperty,
            new DoubleAnimation(PetShadowScale.ScaleX, dragging ? 1.16 : 1, duration));
        PetShadowScale.BeginAnimation(ScaleTransform.ScaleYProperty,
            new DoubleAnimation(PetShadowScale.ScaleY, dragging ? 0.72 : 1, duration));
        if (!dragging) UpdateDragTilt(0, 190);
    }

    private void UpdateDragTilt(double horizontalVelocity, int durationMilliseconds = 85)
    {
        var target = Math.Clamp(horizontalVelocity * 0.008, -6.5, 6.5);
        var duration = TimeSpan.FromMilliseconds(durationMilliseconds);
        var main = new DoubleAnimation(AvatarDragTilt.Angle, target, duration)
        {
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        AvatarDragTilt.BeginAnimation(RotateTransform.AngleProperty, main);
        PetAvatarDragTilt.BeginAnimation(RotateTransform.AngleProperty, main.Clone());
    }

    private void UpdateSnapCandidate(Rect area, double width, double height)
    {
        const double threshold = 54;
        var candidate = DragSnapEdges.None;
        var leftDistance = Math.Abs(Left - area.Left);
        var rightDistance = Math.Abs(Left + width - area.Right);
        var topDistance = Math.Abs(Top - area.Top);
        var bottomDistance = Math.Abs(Top + height - area.Bottom);
        if (Math.Min(leftDistance, rightDistance) <= threshold)
            candidate |= leftDistance <= rightDistance ? DragSnapEdges.Left : DragSnapEdges.Right;
        if (Math.Min(topDistance, bottomDistance) <= threshold)
            candidate |= topDistance <= bottomDistance ? DragSnapEdges.Top : DragSnapEdges.Bottom;
        if (candidate == _dragSnapCandidate) return;
        _dragSnapCandidate = candidate;
        if (candidate == DragSnapEdges.None) HideSnapHint();
        else ShowSnapHint($"松手吸附到{SnapEdgeLabel(candidate)}");
    }

    private static string SnapEdgeLabel(DragSnapEdges edges)
    {
        var horizontal = edges.HasFlag(DragSnapEdges.Left) ? "左" :
            edges.HasFlag(DragSnapEdges.Right) ? "右" : "";
        var vertical = edges.HasFlag(DragSnapEdges.Top) ? "上" :
            edges.HasFlag(DragSnapEdges.Bottom) ? "下" : "";
        if (!string.IsNullOrEmpty(horizontal) && !string.IsNullOrEmpty(vertical))
            return horizontal + vertical + "角";
        return string.IsNullOrEmpty(horizontal + vertical) ? "边缘" : horizontal + vertical + "侧";
    }

    private void ShowSnapHint(string text)
    {
        _snapHintGeneration++;
        SnapHintText.Text = text;
        SnapHintBadge.Visibility = Visibility.Visible;
        SnapHintBadge.BeginAnimation(OpacityProperty,
            new DoubleAnimation(SnapHintBadge.Opacity, 1, TimeSpan.FromMilliseconds(120)));
        var pop = new DoubleAnimation(0.92, 1, TimeSpan.FromMilliseconds(150))
        {
            EasingFunction = new BackEase { Amplitude = 0.2, EasingMode = EasingMode.EaseOut },
        };
        SnapHintScale.BeginAnimation(ScaleTransform.ScaleXProperty, pop);
        SnapHintScale.BeginAnimation(ScaleTransform.ScaleYProperty, pop.Clone());
    }

    private async void HideSnapHint(int delayMilliseconds = 0)
    {
        var generation = ++_snapHintGeneration;
        if (delayMilliseconds > 0) await Task.Delay(delayMilliseconds);
        if (generation != _snapHintGeneration) return;
        var fade = new DoubleAnimation(SnapHintBadge.Opacity, 0, TimeSpan.FromMilliseconds(150));
        fade.Completed += (_, _) =>
        {
            if (generation == _snapHintGeneration) SnapHintBadge.Visibility = Visibility.Collapsed;
        };
        SnapHintBadge.BeginAnimation(OpacityProperty, fade);
    }

    private void CompleteWindowDrag()
    {
        if (!_isWindowDragging) return;
        _isWindowDragging = false;
        if (Mouse.Captured == this) Mouse.Capture(null);
        SetDragVisual(false);
        var area = CurrentWorkingAreaDip();
        var width = ActualWidth > 0 ? ActualWidth : Width;
        var height = ActualHeight > 0 ? ActualHeight : Height;
        var margin = _petMode ? 8d : 6d;
        var minLeft = area.Left + margin;
        var maxLeft = Math.Max(minLeft, area.Right - width - margin);
        var minTop = area.Top + margin;
        var maxTop = Math.Max(minTop, area.Bottom - height - margin);
        var targetLeft = Math.Clamp(Left + Math.Clamp(_dragVelocity.X * 0.1, -86, 86), minLeft, maxLeft);
        var targetTop = Math.Clamp(Top + Math.Clamp(_dragVelocity.Y * 0.1, -72, 72), minTop, maxTop);
        var snapping = _dragSnapCandidate != DragSnapEdges.None;
        if (_dragSnapCandidate.HasFlag(DragSnapEdges.Left)) targetLeft = minLeft;
        if (_dragSnapCandidate.HasFlag(DragSnapEdges.Right)) targetLeft = maxLeft;
        if (_dragSnapCandidate.HasFlag(DragSnapEdges.Top)) targetTop = minTop;
        if (_dragSnapCandidate.HasFlag(DragSnapEdges.Bottom)) targetTop = maxTop;
        if (snapping)
        {
            ShowSnapHint($"已吸附到{SnapEdgeLabel(_dragSnapCandidate)}");
            HideSnapHint(520);
        }
        else HideSnapHint();
        AnimateWindowPosition(targetLeft, targetTop, snapping ? 230 : 180);
        _dragSnapCandidate = DragSnapEdges.None;
    }

    private void AnimateWindowPosition(double targetLeft, double targetTop, int durationMilliseconds)
    {
        var duration = TimeSpan.FromMilliseconds(durationMilliseconds);
        var easing = new CubicEase { EasingMode = EasingMode.EaseOut };
        var leftAnimation = new DoubleAnimation(Left, targetLeft, duration)
        {
            EasingFunction = easing,
            FillBehavior = FillBehavior.Stop,
        };
        var topAnimation = new DoubleAnimation(Top, targetTop, duration)
        {
            EasingFunction = easing,
            FillBehavior = FillBehavior.Stop,
        };
        topAnimation.Completed += (_, _) =>
        {
            BeginAnimation(LeftProperty, null);
            BeginAnimation(TopProperty, null);
            Left = targetLeft;
            Top = targetTop;
            SavePresentationState();
        };
        BeginAnimation(LeftProperty, leftAnimation, HandoffBehavior.SnapshotAndReplace);
        BeginAnimation(TopProperty, topAnimation, HandoffBehavior.SnapshotAndReplace);
    }

    private void SetPetMode(bool enabled, bool save = true)
    {
        if (_petMode == enabled) return;
        var anchorRight = Left + ActualWidth;
        var anchorBottom = Top + ActualHeight;
        _petMode = enabled;
        FullPanelRoot.Visibility = enabled ? Visibility.Collapsed : Visibility.Visible;
        PetModeRoot.Visibility = enabled ? Visibility.Visible : Visibility.Collapsed;
        Width = enabled ? 300 : 460;
        Height = enabled ? 430 : 720;
        var area = SystemParameters.WorkArea;
        Left = Math.Clamp(anchorRight - Width, area.Left, Math.Max(area.Left, area.Right - Width));
        Top = Math.Clamp(anchorBottom - Height, area.Top, Math.Max(area.Top, area.Bottom - Height));
        if (enabled)
        {
            PlayReaction(ReactionCue.Welcome);
            ShowPetBubble(_busy ? "我正在认真想主人刚才说的话…" : "主人，我会安静待在这里陪你喵。", true);
        }
        else
        {
            HidePetBubble();
            InputBox.Focus();
        }
        if (save) SavePresentationState();
    }

    private void TogglePetModeButton_Click(object sender, RoutedEventArgs e) => SetPetMode(!_petMode);

    private void PetAvatar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount >= 2)
        {
            SetPetMode(false);
            e.Handled = true;
            return;
        }
        BeginWindowDrag(e);
    }

    private void PetAvatar_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
    {
        var hover = new DoubleAnimation(PetAvatarHoverScale.ScaleX, 1.025, TimeSpan.FromMilliseconds(160))
        {
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        PetAvatarHoverScale.BeginAnimation(ScaleTransform.ScaleXProperty, hover);
        PetAvatarHoverScale.BeginAnimation(ScaleTransform.ScaleYProperty, hover.Clone());
    }

    private void PetAvatar_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
    {
        var leave = new DoubleAnimation(PetAvatarHoverScale.ScaleX, 1, TimeSpan.FromMilliseconds(180))
        {
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        PetAvatarHoverScale.BeginAnimation(ScaleTransform.ScaleXProperty, leave);
        PetAvatarHoverScale.BeginAnimation(ScaleTransform.ScaleYProperty, leave.Clone());
    }

    private async Task SendQuickGestureAsync(string text)
    {
        if (!_connected)
        {
            ShowPetBubble("核心还在连接……主人稍等我一下喵。", true);
            return;
        }
        if (_busy)
        {
            ShowPetBubble("我还在认真组织上一句话，马上就好喵。", true);
            return;
        }
        _busy = true;
        InputBox.IsEnabled = false;
        RequestExpression(AvatarExpression.Shy, 3.5, true);
        PlayReaction(ReactionCue.Headpat);
        AppendChat($"\n\n主人\n{text}", true);
        ShowPetBubble("唔……主人的手很温柔。", false);
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
            ShowPetBubble("刚刚没能把回应送出来……连接正在恢复。", true);
            ShowToast(ex.Message, "#FFF0EC");
        }
    }

    private async void PetHeadpatButton_Click(object sender, RoutedEventArgs e) =>
        await SendQuickGestureAsync("摸摸头");

    private static double GetIdleSeconds()
    {
        var info = new LastInputInfo { Size = (uint)Marshal.SizeOf<LastInputInfo>() };
        if (!GetLastInputInfo(ref info)) return 0;
        var current = unchecked((uint)Environment.TickCount);
        return unchecked(current - info.Time) / 1000.0;
    }

    private bool IsForegroundFullScreen()
    {
        var handle = GetForegroundWindow();
        if (handle == 0 || handle == new WindowInteropHelper(this).Handle || !GetWindowRect(handle, out var rect))
            return false;
        var screen = Forms.Screen.FromHandle(handle).Bounds;
        const int tolerance = 4;
        return Math.Abs(rect.Left - screen.Left) <= tolerance &&
               Math.Abs(rect.Top - screen.Top) <= tolerance &&
               Math.Abs(rect.Right - screen.Right) <= tolerance &&
               Math.Abs(rect.Bottom - screen.Bottom) <= tolerance;
    }

    private async Task SendPresencePulseAsync()
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "presence.pulse",
                request_id = Guid.NewGuid().ToString("N"),
                idle_seconds = GetIdleSeconds(),
                visible = true,
                window_visible = IsVisible,
                full_screen = IsForegroundFullScreen(),
            });
        }
        catch
        {
            SetConnected(false);
        }
    }

    private async Task SendProactiveFeedbackAsync(string action)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentProactiveCandidateId)) return;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "proactive.feedback",
                request_id = Guid.NewGuid().ToString("N"),
                candidate_id = _currentProactiveCandidateId,
                action,
            });
            ProactiveBubbleActions.Visibility = Visibility.Collapsed;
            RefreshBubbleHistoryControls();
            if (action != "reply") HidePetBubble();
            _currentProactiveCandidateId = string.Empty;
            if (action != "reply")
            {
                _currentProactiveProjectId = string.Empty;
                _currentProactiveOpportunityId = string.Empty;
                _currentProactiveKind = string.Empty;
                ProactiveReplyButton.Content = "回应";
            }
        }
        catch (Exception ex)
        {
            ProactiveFeedbackText.Text = ex.Message;
        }
    }

    private async void ProactiveReplyButton_Click(object sender, RoutedEventArgs e)
    {
        var projectId = _currentProactiveProjectId;
        var opportunityId = _currentProactiveOpportunityId;
        var proactiveKind = _currentProactiveKind;
        var candidateId = _currentProactiveCandidateId;
        await SendProactiveFeedbackAsync("reply");
        _currentProactiveProjectId = string.Empty;
        _currentProactiveOpportunityId = string.Empty;
        _currentProactiveKind = string.Empty;
        if (proactiveKind is "suggestion" or "digest" && !string.IsNullOrWhiteSpace(candidateId))
        {
            _pendingEvidenceProjectId = projectId;
            _pendingEvidenceOpportunityId = opportunityId;
            await _pipe.SendAsync(new
            {
                type = "proactive.details",
                request_id = Guid.NewGuid().ToString("N"),
                candidate_id = candidateId,
            });
        }
        else
        {
            SetPetMode(false);
            MainTabs.SelectedIndex = 0;
            InputBox.Focus();
        }
    }

    private async void ProactiveLaterButton_Click(object sender, RoutedEventArgs e) =>
        await SendProactiveFeedbackAsync("later");

    private async void ProactiveDismissButton_Click(object sender, RoutedEventArgs e) =>
        await SendProactiveFeedbackAsync("dismiss");

    private async void SaveProactiveSettingsButton_Click(object sender, RoutedEventArgs e)
    {
        var selected = ProactiveBudgetBox.SelectedItem as System.Windows.Controls.ComboBoxItem;
        var budget = int.TryParse(selected?.Tag?.ToString(), out var parsed) ? parsed : 3;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "proactive.settings",
                request_id = Guid.NewGuid().ToString("N"),
                enabled = ProactiveEnabledBox.IsChecked == true,
                daily_budget = budget,
            });
            ProactiveFeedbackText.Text = "主动陪伴设置已经保存。";
        }
        catch (Exception ex)
        {
            ProactiveFeedbackText.Text = ex.Message;
        }
    }

    private async void ClearProactiveMutesButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        await _pipe.SendAsync(new { type = "proactive.clear_mutes", request_id = Guid.NewGuid().ToString("N") });
        ProactiveFeedbackText.Text = "之前点过“别再提醒”的事项可以重新进入候选了。";
    }

    private void ProactiveListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        var selected = ProactiveListBox.SelectedItem as ProactiveViewItem;
        ResolveProactiveLoopButton.IsEnabled = selected is not null;
        PostponeProactiveLoopButton.IsEnabled = selected is not null && !selected.DueText.Contains("不自动追问");
        DismissProactiveLoopButton.IsEnabled = selected is not null;
    }

    private async Task SendProactiveLoopActionAsync(string action, int postponeSeconds = 86400)
    {
        if (!_connected || ProactiveListBox.SelectedItem is not ProactiveViewItem selected) return;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "proactive.loop_action",
                request_id = Guid.NewGuid().ToString("N"),
                loop_id = selected.LoopId,
                action,
                postpone_seconds = postponeSeconds,
            });
            ProactiveFeedbackText.Text = action switch
            {
                "resolve" => "这件事已经标记为解决，不会再回访。",
                "postpone" => "这件事已经延后一天。",
                _ => "这件事已经关闭，不会再询问。",
            };
        }
        catch (Exception ex)
        {
            ProactiveFeedbackText.Text = ex.Message;
        }
    }

    private async void ResolveProactiveLoopButton_Click(object sender, RoutedEventArgs e) =>
        await SendProactiveLoopActionAsync("resolve");

    private async void PostponeProactiveLoopButton_Click(object sender, RoutedEventArgs e) =>
        await SendProactiveLoopActionAsync("postpone");

    private async void DismissProactiveLoopButton_Click(object sender, RoutedEventArgs e) =>
        await SendProactiveLoopActionAsync("dismiss");

    private async void QuietTodayButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        await _pipe.SendAsync(new
        {
            type = "proactive.quiet_today",
            request_id = Guid.NewGuid().ToString("N"),
            hours = 12,
        });
        ProactiveFeedbackText.Text = "未名子会安静十二小时；任务仍会正常执行，但不会主动弹出。";
    }

    private async void ResetProactiveHabitsButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected) return;
        await _pipe.SendAsync(new { type = "proactive.reset_habits", request_id = Guid.NewGuid().ToString("N") });
        ProactiveFeedbackText.Text = "主动节奏已经恢复默认，待续事项不会被删除。";
    }

    private async void RefreshProactiveButton_Click(object sender, RoutedEventArgs e) => await RefreshProactiveAsync();

    private void ProjectListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (ProjectListBox.SelectedItem is not ProjectViewItem project)
        {
            ProjectDetailTitleText.Text = "尚未选择项目";
            ProjectGoalText.Text = "";
            ProjectProgressText.Text = "";
            ProjectArtifactListBox.ItemsSource = null;
            ProjectOpportunityListBox.ItemsSource = null;
            return;
        }
        ProjectDetailTitleText.Text = $"{project.Title} · {project.Status}";
        ProjectGoalText.Text = string.IsNullOrWhiteSpace(project.Goal) ? "没有补充目标说明" : project.Goal;
        ProjectProgressText.Text = project.Progress;
        ProjectArtifactListBox.ItemsSource = project.Artifacts;
        ProjectArtifactListBox.SelectedItem = project.Artifacts.FirstOrDefault();
        ProjectOpportunityListBox.ItemsSource = project.Opportunities;
        ProjectOpportunityListBox.SelectedItem = project.Opportunities.FirstOrDefault();
        ArchiveProjectButton.Content = project.Archived ? "恢复项目" : "归档项目";
        ProjectFeedbackText.Text = project.Archived ? "这个项目已经归档。" : "所有下一步都只会先生成计划预览。";
    }

    private void ProjectOpportunityListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        var opportunity = ProjectOpportunityListBox.SelectedItem as ProjectOpportunityViewItem;
        PlanProjectOpportunityButton.IsEnabled = opportunity is not null;
        LaterProjectOpportunityButton.IsEnabled = opportunity is not null;
        DismissProjectOpportunityButton.IsEnabled = opportunity is not null;
        ProjectOpportunityEvidenceText.Text = opportunity is null
            ? ""
            : $"价值评分：{opportunity.ValueScore:P0}\n依据：\n{opportunity.Evidence}\n风险与权限：{opportunity.Risk}";
    }

    private async Task SendProjectOpportunityActionAsync(string action)
    {
        if (!_connected || ProjectOpportunityListBox.SelectedItem is not ProjectOpportunityViewItem opportunity) return;
        try
        {
            await _pipe.SendAsync(new
            {
                type = "project.opportunity_action",
                request_id = Guid.NewGuid().ToString("N"),
                project_id = opportunity.ProjectId,
                opportunity_id = opportunity.Id,
                action,
            });
            ProjectFeedbackText.Text = action switch
            {
                "plan" => "正在把建议放入任务目标框；尚未生成或执行计划。",
                "later" => "这条建议已经延后一天。",
                _ => "这条建议已经忽略，不会重复提出。",
            };
        }
        catch (Exception ex)
        {
            ProjectFeedbackText.Text = ex.Message;
        }
    }

    private async void PlanProjectOpportunityButton_Click(object sender, RoutedEventArgs e) =>
        await SendProjectOpportunityActionAsync("plan");

    private async void LaterProjectOpportunityButton_Click(object sender, RoutedEventArgs e) =>
        await SendProjectOpportunityActionAsync("later");

    private async void DismissProjectOpportunityButton_Click(object sender, RoutedEventArgs e) =>
        await SendProjectOpportunityActionAsync("dismiss");

    private void OpenProjectArtifactButton_Click(object sender, RoutedEventArgs e)
    {
        if (ProjectArtifactListBox.SelectedItem is not ProjectArtifactViewItem artifact || string.IsNullOrWhiteSpace(_workspacePath))
        {
            ProjectFeedbackText.Text = "没有可打开的项目产物。";
            return;
        }
        try
        {
            var root = Path.GetFullPath(_workspacePath).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var target = Path.GetFullPath(Path.Combine(root, artifact.Path));
            if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                ProjectFeedbackText.Text = "产物路径不在专属工作区内，已拒绝打开。";
                return;
            }
            OpenLocalPath(target);
        }
        catch (Exception ex)
        {
            ProjectFeedbackText.Text = ex.Message;
        }
    }

    private async void ArchiveProjectButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || ProjectListBox.SelectedItem is not ProjectViewItem project) return;
        await _pipe.SendAsync(new
        {
            type = "project.archive",
            request_id = Guid.NewGuid().ToString("N"),
            project_id = project.Id,
            archived = !project.Archived,
        });
    }

    private async void RefreshProjectsButton_Click(object sender, RoutedEventArgs e) => await RefreshProjectsAsync();

    private async Task SendAutonomyAsync(object payload)
    {
        if (!_connected) return;
        try
        {
            await _pipe.SendAsync(payload);
        }
        catch (Exception ex)
        {
            AutonomyFeedbackText.Text = ex.Message;
        }
    }

    private async void GrantGlobalAutonomyButton_Click(object sender, RoutedEventArgs e)
    {
        await SendAutonomyAsync(new {
            type = "autonomy.grant", request_id = Guid.NewGuid().ToString("N"),
            project_id = "", valid_days = 30, max_files_per_day = 3,
        });
        AutonomyFeedbackText.Text = "已请求启用 L1 安全草稿能力：30 天有效，每天最多 3 份。";
    }

    private async void GrantProjectAutonomyButton_Click(object sender, RoutedEventArgs e)
    {
        if (ProjectListBox.SelectedItem is not ProjectViewItem project)
        {
            AutonomyFeedbackText.Text = "请先到“项目”页选中一个项目。";
            return;
        }
        await SendAutonomyAsync(new {
            type = "autonomy.grant", request_id = Guid.NewGuid().ToString("N"),
            project_id = project.Id, valid_days = 30, max_files_per_day = 3,
        });
        AutonomyFeedbackText.Text = $"已请求只为“{project.Title}”启用 L2 安全草稿能力。";
    }

    private async void GrantNetworkAutonomyButton_Click(object sender, RoutedEventArgs e)
    {
        var project = ProjectListBox.SelectedItem as ProjectViewItem;
        await SendAutonomyAsync(new {
            type = "autonomy.network_grant", request_id = Guid.NewGuid().ToString("N"),
            project_id = project?.Id ?? "", valid_days = 7, max_requests_per_day = 2,
        });
        AutonomyFeedbackText.Text = project is null
            ? "已授权 7 天的全局只读网络研究，每天最多 2 次。"
            : $"已授权“{project.Title}”进行 7 天只读网络研究，每天最多 2 次。";
    }

    private async Task GrantSelectedProjectPackageAsync(string mode)
    {
        if (ProjectListBox.SelectedItem is not ProjectViewItem project)
        {
            AutonomyFeedbackText.Text = "请先到“项目”页选中一个项目。";
            return;
        }
        await SendAutonomyAsync(new {
            type = "autonomy.package_grant", request_id = Guid.NewGuid().ToString("N"),
            project_id = project.Id, mode, valid_days = 7,
        });
        AutonomyFeedbackText.Text = mode == "research_helper"
            ? $"已为“{project.Title}”启用 7 天研究助手包：每天最多 1 次只读网络研究和 2 次低风险草稿。"
            : $"已为“{project.Title}”启用 7 天项目陪跑包：只会在授权范围内准备可丢弃草稿。";
    }

    private async void GrantProjectPackageButton_Click(object sender, RoutedEventArgs e) =>
        await GrantSelectedProjectPackageAsync("project_helper");

    private async void GrantResearchPackageButton_Click(object sender, RoutedEventArgs e) =>
        await GrantSelectedProjectPackageAsync("research_helper");

    private async void RevokePackageButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyPackageListBox.SelectedItem is not AutonomyPackageViewItem package || package.Status != "active") return;
        await SendAutonomyAsync(new {
            type = "autonomy.package_revoke", request_id = Guid.NewGuid().ToString("N"), package_id = package.Id,
        });
        AutonomyFeedbackText.Text = "委托权限包已撤销；由它单独创建的能力卡和待处理工作也一起取消了。";
    }

    private void AutonomyPackageListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (RevokePackageButton is not null)
            RevokePackageButton.IsEnabled = AutonomyPackageListBox.SelectedItem is AutonomyPackageViewItem package && package.Status == "active";
    }

    private async void ResetCircuitButton_Click(object sender, RoutedEventArgs e)
    {
        await SendAutonomyAsync(new { type = "autonomy.circuit_reset", request_id = Guid.NewGuid().ToString("N") });
        AutonomyFeedbackText.Text = "自主行动熔断已经解除；下一次仍会完整检查情境、权限和安全边界。";
    }

    private async void PauseAutonomyButton_Click(object sender, RoutedEventArgs e) =>
        await SendAutonomyAsync(new {
            type = "autonomy.pause", request_id = Guid.NewGuid().ToString("N"), paused = !_autonomyPaused,
        });

    private async void RunAutonomyButton_Click(object sender, RoutedEventArgs e) =>
        await SendAutonomyAsync(new { type = "autonomy.run_now", request_id = Guid.NewGuid().ToString("N") });

    private async void RevokeAutonomyButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyGrantListBox.SelectedItem is not AutonomyGrantViewItem grant || grant.Status != "active") return;
        await SendAutonomyAsync(new {
            type = "autonomy.revoke", request_id = Guid.NewGuid().ToString("N"), grant_id = grant.Id,
        });
        AutonomyFeedbackText.Text = "能力卡已撤销；它会从当前列表移入历史能力卡。";
    }

    private void AutonomyGrantListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (RevokeAutonomyButton is not null)
            RevokeAutonomyButton.IsEnabled = AutonomyGrantListBox.SelectedItem is AutonomyGrantViewItem grant &&
                                               grant.Status == "active";
    }

    private void AutonomyJobListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        AutonomyJobDetailText.Text = AutonomyJobListBox.SelectedItem is AutonomyJobViewItem job
            ? job.Detail : "尚未选择草稿。";
    }

    private void OpenAutonomyDraftButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyJobListBox.SelectedItem is not AutonomyJobViewItem job || string.IsNullOrWhiteSpace(job.Path))
        {
            AutonomyFeedbackText.Text = "这项工作还没有可打开的草稿。";
            return;
        }
        try
        {
            var root = Path.GetFullPath(_autonomyDraftsDir).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var path = Path.GetFullPath(job.Path);
            if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            {
                AutonomyFeedbackText.Text = "草稿路径不在专属草稿区，已拒绝打开。";
                return;
            }
            OpenLocalPath(path);
        }
        catch (Exception ex)
        {
            AutonomyFeedbackText.Text = ex.Message;
        }
    }

    private async void AdoptAutonomyDraftButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyJobListBox.SelectedItem is not AutonomyJobViewItem job || job.Status != "completed")
        {
            AutonomyFeedbackText.Text = "只有已生成并通过验证的草稿可以申请采纳。";
            return;
        }
        await SendAutonomyAsync(new {
            type = "autonomy.adopt", request_id = Guid.NewGuid().ToString("N"), job_id = job.Id,
        });
        AutonomyFeedbackText.Text = "已经提交采纳申请，请在任务页检查权限确认卡。";
        MainTabs.SelectedIndex = 1;
    }

    private async void DiscardAutonomyDraftButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyJobListBox.SelectedItem is not AutonomyJobViewItem job) return;
        await SendAutonomyAsync(new {
            type = "autonomy.discard", request_id = Guid.NewGuid().ToString("N"), job_id = job.Id,
        });
    }

    private async Task SendSelectedAutonomyFeedbackAsync(string action)
    {
        if (AutonomyJobListBox.SelectedItem is not AutonomyJobViewItem job)
        {
            AutonomyFeedbackText.Text = "请先选择一份自主工作。";
            return;
        }
        await SendAutonomyAsync(new {
            type = "autonomy.feedback", request_id = Guid.NewGuid().ToString("N"), job_id = job.Id, action,
        });
        AutonomyFeedbackText.Text = action switch {
            "more" => "已经记住：以后会提高这类工作的优先级。",
            "less" => "已经记住：以后会降低这类工作的优先级。",
            _ => "已经记住：以后不再主动做此项目中的这类工作。",
        };
    }

    private async void MoreAutonomyFeedbackButton_Click(object sender, RoutedEventArgs e) =>
        await SendSelectedAutonomyFeedbackAsync("more");
    private async void LessAutonomyFeedbackButton_Click(object sender, RoutedEventArgs e) =>
        await SendSelectedAutonomyFeedbackAsync("less");
    private async void NeverAutonomyFeedbackButton_Click(object sender, RoutedEventArgs e) =>
        await SendSelectedAutonomyFeedbackAsync("never");

    private void AutonomyInboxListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        AutonomyInboxDetailText.Text = AutonomyInboxListBox.SelectedItem is AutonomyInboxViewItem item
            ? item.Detail : "尚未选择收件箱记录。";
    }

    private async void AcknowledgeAutonomyInboxButton_Click(object sender, RoutedEventArgs e)
    {
        if (AutonomyInboxListBox.SelectedItem is not AutonomyInboxViewItem item) return;
        await SendAutonomyAsync(new {
            type = "autonomy.inbox_ack", request_id = Guid.NewGuid().ToString("N"), inbox_id = item.Id,
        });
    }

    private async void ResetAutonomyPreferencesButton_Click(object sender, RoutedEventArgs e) =>
        await SendAutonomyAsync(new { type = "autonomy.preferences_reset", request_id = Guid.NewGuid().ToString("N") });

    private void OpenAutonomyDirectoryButton_Click(object sender, RoutedEventArgs e) => OpenLocalPath(_autonomyDraftsDir);

    private async void RefreshAutonomyButton_Click(object sender, RoutedEventArgs e) => await RefreshAutonomyAsync();

    private void PetChatButton_Click(object sender, RoutedEventArgs e)
    {
        SetPetMode(false);
        InputBox.Focus();
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

    private async void CreateReminderButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || !int.TryParse(ReminderDelayBox.Text, out var delay) || delay < 1)
        {
            TaskFeedbackText.Text = "请输入至少 1 分钟的提醒时间。";
            return;
        }
        await _pipe.SendAsync(new
        {
            type = "reminder.create",
            request_id = Guid.NewGuid().ToString("N"),
            title = ReminderTitleBox.Text,
            message = ReminderMessageBox.Text,
            delay_minutes = delay,
        });
    }

    private async void CancelReminderButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || ReminderListBox.SelectedItem is not ReminderViewItem item) return;
        await _pipe.SendAsync(new
        {
            type = "reminder.cancel",
            request_id = Guid.NewGuid().ToString("N"),
            reminder_id = item.Id,
        });
    }

    private async void CreateGoalPlanButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected)
        {
            TaskFeedbackText.Text = "桌面核心尚未连接，请稍等。";
            return;
        }
        var goal = GoalTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(goal))
        {
            TaskFeedbackText.Text = "请先写下一个自然语言目标。";
            return;
        }
        CreateGoalPlanButton.IsEnabled = false;
        TaskFeedbackText.Text = "正在提交目标…";
        try
        {
            await _pipe.SendAsync(new
            {
                type = "goal.plan",
                request_id = Guid.NewGuid().ToString("N"),
                goal,
                source_project_id = _pendingSourceProjectId,
                source_opportunity_id = _pendingSourceOpportunityId,
            });
        }
        catch (Exception ex)
        {
            CreateGoalPlanButton.IsEnabled = true;
            TaskFeedbackText.Text = ex.Message;
        }
    }

    private async Task DecidePlanAsync(bool confirm)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentPlanTaskId)) return;
        var taskId = _currentPlanTaskId;
        TaskFeedbackText.Text = confirm ? "正在确认计划…" : "正在放弃计划…";
        try
        {
            await _pipe.SendAsync(new
            {
                type = confirm ? "plan.confirm" : "plan.reject",
                request_id = Guid.NewGuid().ToString("N"),
                task_id = taskId,
            });
        }
        catch (Exception ex)
        {
            TaskFeedbackText.Text = ex.Message;
        }
    }

    private async void ConfirmPlanButton_Click(object sender, RoutedEventArgs e) =>
        await DecidePlanAsync(true);

    private async void RejectPlanButton_Click(object sender, RoutedEventArgs e) =>
        await DecidePlanAsync(false);

    private async void RegeneratePlanButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentPlanTaskId)) return;
        var goal = GoalTextBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(goal))
        {
            TaskFeedbackText.Text = "原目标输入框为空，无法重新生成。";
            return;
        }
        await _pipe.SendAsync(new
        {
            type = "plan.regenerate",
            request_id = Guid.NewGuid().ToString("N"),
            task_id = _currentPlanTaskId,
            goal,
        });
    }

    private async void SavePlanEditButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentPlanTaskId)) return;
        if (string.IsNullOrWhiteSpace(PlanEditContentBox.Text))
        {
            TaskFeedbackText.Text = "研究型计划会在执行后生成内容，暂时只能编辑标题和路径；普通计划内容不能为空。";
            return;
        }
        await _pipe.SendAsync(new
        {
            type = "plan.edit_output",
            request_id = Guid.NewGuid().ToString("N"),
            task_id = _currentPlanTaskId,
            title = PlanEditTitleBox.Text,
            relative_path = PlanEditPathBox.Text,
            content = PlanEditContentBox.Text,
        });
    }

    private async void SavePresentationPlanButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || string.IsNullOrWhiteSpace(_currentPlanTaskId) || !_currentPlanIsPresentation) return;
        if (string.IsNullOrWhiteSpace(PlanPptTitleBox.Text) ||
            string.IsNullOrWhiteSpace(PlanPptPathBox.Text) ||
            string.IsNullOrWhiteSpace(PlanPptOutlineBox.Text))
        {
            TaskFeedbackText.Text = "PPT 标题、保存位置和逐页大纲都不能为空。";
            return;
        }
        var layoutStrategy = (PlanPptTemplateBox.SelectedItem as System.Windows.Controls.ComboBoxItem)?.Tag?.ToString()
            ?? "auto_grid";
        var brandTemplate = (PlanPptBrandTemplateBox.SelectedItem as System.Windows.Controls.ComboBoxItem)?.Tag?.ToString()
            ?? "codex_grid";
        await _pipe.SendAsync(new
        {
            type = "plan.edit_presentation",
            request_id = Guid.NewGuid().ToString("N"),
            task_id = _currentPlanTaskId,
            title = PlanPptTitleBox.Text,
            relative_path = PlanPptPathBox.Text,
            template = layoutStrategy,
            layout_strategy = layoutStrategy,
            brand_template = brandTemplate,
            outline = PlanPptOutlineBox.Text,
        });
    }

    private async Task SendTaskActionAsync(string action)
    {
        if (!_connected || TaskListBox.SelectedItem is not TaskViewItem item) return;
        await _pipe.SendAsync(new
        {
            type = $"task.{action}",
            request_id = Guid.NewGuid().ToString("N"),
            task_id = item.Id,
        });
    }

    private async void PauseTaskButton_Click(object sender, RoutedEventArgs e) => await SendTaskActionAsync("pause");
    private async void ResumeTaskButton_Click(object sender, RoutedEventArgs e) => await SendTaskActionAsync("resume");
    private async void RetryTaskButton_Click(object sender, RoutedEventArgs e) => await SendTaskActionAsync("retry");
    private async void CancelTaskButton_Click(object sender, RoutedEventArgs e) => await SendTaskActionAsync("cancel");

    private void TaskListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (TaskListBox.SelectedItem is TaskViewItem item)
            TaskFeedbackText.Text = $"已选择：{item.Display.Replace("\n", " · ")}";
    }

    private void OpenTaskResultButton_Click(object sender, RoutedEventArgs e)
    {
        if (TaskListBox.SelectedItem is not TaskViewItem item || string.IsNullOrWhiteSpace(item.ResultPath))
        {
            TaskFeedbackText.Text = "这个任务还没有可打开的文件结果。";
            return;
        }
        OpenLocalPath(item.ResultPath);
    }

    private void OpenWorkspaceButton_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_workspacePath))
        {
            TaskFeedbackText.Text = "尚未取得专属工作区位置。";
            return;
        }
        OpenLocalPath(_workspacePath);
    }

    private static void OpenLocalPath(string path)
    {
        if (!File.Exists(path) && !Directory.Exists(path)) return;
        Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
    }

    private async void RefreshMemoriesButton_Click(object sender, RoutedEventArgs e) => await RefreshMemoriesAsync();

    private async void MemoryStatusBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (MemoryEditBox is not null) MemoryEditBox.Clear();
        await RefreshMemoriesAsync();
    }

    private void MemoryListBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (MemoryListBox.SelectedItem is MemoryViewItem item) MemoryEditBox.Text = item.Content;
    }

    private async void ReviseMemoryButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || MemoryListBox.SelectedItem is not MemoryViewItem item) return;
        await _pipe.SendAsync(new
        {
            type = "memory.revise",
            request_id = Guid.NewGuid().ToString("N"),
            memory_id = item.Id,
            content = MemoryEditBox.Text,
        });
    }

    private async void ToggleMemoryPinButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || MemoryListBox.SelectedItem is not MemoryViewItem item) return;
        await _pipe.SendAsync(new
        {
            type = "memory.pin",
            request_id = Guid.NewGuid().ToString("N"),
            memory_id = item.Id,
            pinned = !item.Pinned,
        });
    }

    private async void ForgetMemoryButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || MemoryListBox.SelectedItem is not MemoryViewItem item) return;
        var decision = System.Windows.MessageBox.Show(
            "要彻底遗忘选中的记忆吗？这个操作不会自动恢复。",
            "确认彻底遗忘",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning);
        if (decision != MessageBoxResult.Yes) return;
        await _pipe.SendAsync(new
        {
            type = "memory.forget",
            request_id = Guid.NewGuid().ToString("N"),
            memory_id = item.Id,
        });
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

    private void TrackExternalWindow()
    {
        var foreground = GetForegroundWindow();
        var own = new WindowInteropHelper(this).Handle;
        if (foreground == nint.Zero || foreground == own) return;
        if (foreground == _lastExternalWindow) return;
        _lastExternalWindow = foreground;
        _currentExternalTitle = ReadWindowTitle(foreground);
        _currentExternalProcess = _perception.GetProcessName(foreground);
        var app = string.IsNullOrWhiteSpace(_currentExternalProcess) ? "未知应用" : _currentExternalProcess;
        var title = string.IsNullOrWhiteSpace(_currentExternalTitle) ? "（无窗口标题）" : _currentExternalTitle;
        CurrentTargetText.Text = $"当前目标：{app} · {title}";
        UpdatePerceptionIndicator();
    }

    private static string ReadWindowTitle(nint handle)
    {
        var buffer = new StringBuilder(512);
        return handle != nint.Zero && GetWindowText(handle, buffer, buffer.Capacity) > 0
            ? buffer.ToString().Trim() : string.Empty;
    }

    private void RenderPerceptionPolicy()
    {
        PerceptionModeBox.SelectedIndex = _perceptionPolicy.Mode switch
        {
            PerceptionMode.Privacy => 0,
            PerceptionMode.Agent => 2,
            _ => 1,
        };
        TrustedAppsBox.Text = _perceptionPolicy.TrustedAppsText;
        BlockedAppsBox.Text = _perceptionPolicy.BlockedAppsText;
        SendPerceptionToModelBox.IsChecked = _perceptionPolicy.SendSummariesToModel;
        _pausePerceptionItem.Text = _perceptionPolicy.Paused ? "恢复桌面感知" : "暂停桌面感知";
        _pausePerceptionItem.Checked = _perceptionPolicy.Paused;
        UpdatePerceptionIndicator();
    }

    private void SavePerceptionPolicyButton_Click(object sender, RoutedEventArgs e)
    {
        var selected = PerceptionModeBox.SelectedItem as System.Windows.Controls.ComboBoxItem;
        _perceptionPolicy.Mode = selected?.Tag?.ToString() switch
        {
            "privacy" => PerceptionMode.Privacy,
            "agent" => PerceptionMode.Agent,
            _ => PerceptionMode.Companion,
        };
        _perceptionPolicy.SetTrustedApps(TrustedAppsBox.Text);
        _perceptionPolicy.SetBlockedApps(BlockedAppsBox.Text);
        _perceptionPolicy.SendSummariesToModel = SendPerceptionToModelBox.IsChecked == true;
        SavePresentationState();
        RenderPerceptionPolicy();
        PerceptionFeedbackText.Text = _perceptionPolicy.Mode switch
        {
            PerceptionMode.Privacy => "已切换到隐私模式：每次读取都需要单独勾选。",
            PerceptionMode.Agent => "已切换到 Agent 模式：限时任务内可以连续感知，操作权限仍单独确认。",
            _ => "已切换到陪伴模式：点击读取即授权，也可以直接说“看看这个窗口”。",
        };
    }

    private void SetPerceptionPaused(bool paused)
    {
        _perceptionPolicy.Paused = paused;
        if (paused) StopObservation("桌面感知已暂停");
        SavePresentationState();
        RenderPerceptionPolicy();
        var message = paused ? "桌面感知已暂停；未名子不会读取窗口内容。" : "桌面感知已经恢复。";
        PerceptionFeedbackText.Text = message;
        ShowToast(message, paused ? "#FFF8E8" : "#EFF8F1");
    }

    private void UpdatePerceptionIndicator()
    {
        if (_observationTimer.IsEnabled) return;
        PerceptionIndicator.Visibility = Visibility.Visible;
        if (_perceptionPolicy.Paused)
        {
            PerceptionIndicatorText.Text = "Ⅱ 感知暂停";
            PerceptionIndicator.Background = new SolidColorBrush(
                (System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString("#FFF2D8"));
            return;
        }
        PerceptionIndicator.Background = new SolidColorBrush(
            (System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString("#EAF4EC"));
        if (_perceptionPolicy.Mode == PerceptionMode.Privacy)
        {
            PerceptionIndicatorText.Text = "○ 隐私模式";
            return;
        }
        var app = string.IsNullOrWhiteSpace(_currentExternalProcess) ? "等待窗口" : _currentExternalProcess;
        PerceptionIndicatorText.Text = $"◉ {app}";
    }

    private bool CanReadFromButton(bool checkedConsent, string permissionName, bool appScoped = true)
    {
        if (_perceptionPolicy.Paused)
        {
            PerceptionFeedbackText.Text = "桌面感知已暂停，可以从托盘菜单恢复。";
            return false;
        }
        if (appScoped && _perceptionPolicy.IsBlocked(_currentExternalProcess))
        {
            PerceptionFeedbackText.Text = $"{_currentExternalProcess} 位于禁止名单，未读取任何内容。";
            return false;
        }
        if (_perceptionPolicy.Mode == PerceptionMode.Privacy && !checkedConsent)
        {
            PerceptionFeedbackText.Text = $"隐私模式下请先勾选一次性{permissionName}许可。";
            return false;
        }
        return true;
    }

    private void AddPerceptionContext(string context)
    {
        var clean = context.Trim();
        if (clean.Length > 5000) clean = clean[..5000] + "…";
        var combined = string.IsNullOrWhiteSpace(_pendingPerceptionContext)
            ? clean : _pendingPerceptionContext + "\n\n" + clean;
        if (combined.Length > 12000)
        {
            combined = "【较早的观察结果已从预览中省略】\n" + combined[^11500..];
        }
        _pendingPerceptionContext = combined;
        PerceptionPreviewBox.Text = _pendingPerceptionContext;
        PerceptionPreviewBox.ScrollToEnd();
    }

    private void ClearPerceptionContext()
    {
        _pendingPerceptionContext = string.Empty;
        PerceptionPreviewBox.Clear();
        ClipboardConsentBox.IsChecked = false;
        WindowConsentBox.IsChecked = false;
        StructureConsentBox.IsChecked = false;
        ScreenshotConsentBox.IsChecked = false;
    }

    private void CaptureClipboardButton_Click(object sender, RoutedEventArgs e)
    {
        if (!CanReadFromButton(ClipboardConsentBox.IsChecked == true, "剪贴板", appScoped: false)) return;
        try
        {
            var text = System.Windows.Clipboard.ContainsText() ? System.Windows.Clipboard.GetText() : string.Empty;
            if (string.IsNullOrWhiteSpace(text)) throw new InvalidOperationException("剪贴板里没有文本");
            AddPerceptionContext($"【剪贴板文本】\n{(text.Length > 4000 ? text[..4000] + "…" : text)}");
            ClipboardConsentBox.IsChecked = false;
            PerceptionFeedbackText.Text = "已读取一次并显示预览；发送下一条消息后自动清除。";
        }
        catch (Exception ex)
        {
            PerceptionFeedbackText.Text = ex.Message;
        }
    }

    private void CaptureWindowButton_Click(object sender, RoutedEventArgs e)
    {
        if (!CanReadFromButton(WindowConsentBox.IsChecked == true, "窗口标题")) return;
        var title = ReadWindowTitle(_lastExternalWindow);
        if (string.IsNullOrWhiteSpace(title))
        {
            PerceptionFeedbackText.Text = "没有取得最近活动窗口的标题。";
            return;
        }
        AddPerceptionContext($"【最近活动窗口标题】{title}");
        WindowConsentBox.IsChecked = false;
        PerceptionFeedbackText.Text = "已读取一次并显示预览。";
    }

    private async void CaptureStructureButton_Click(object sender, RoutedEventArgs e)
    {
        if (!CanReadFromButton(StructureConsentBox.IsChecked == true, "控件结构")) return;
        var handle = _lastExternalWindow;
        if (handle == nint.Zero || !DesktopPerceptionService.IsWindow(handle))
        {
            PerceptionFeedbackText.Text = "没有找到可以读取的最近活动窗口。";
            return;
        }
        StructureConsentBox.IsChecked = false;
        PerceptionFeedbackText.Text = "正在读取窗口控件结构…";
        try
        {
            var snapshot = await Task.Run(() => _perception.CaptureStructure(handle));
            AddPerceptionContext(snapshot.ToContext());
            PerceptionFeedbackText.Text = snapshot.Controls.Count > 0
                ? $"读取到 {snapshot.Controls.Count} 个控件；内容已进入预览。"
                : "这个窗口没有开放可用控件；可以改用截图识别。";
        }
        catch (Exception ex)
        {
            PerceptionFeedbackText.Text = $"控件结构读取失败：{ex.Message}";
        }
    }

    private async void CaptureScreenshotButton_Click(object sender, RoutedEventArgs e)
    {
        if (!_connected || !CanReadFromButton(ScreenshotConsentBox.IsChecked == true, "截图"))
        {
            if (!_connected) PerceptionFeedbackText.Text = "桌面核心尚未连接，暂时不能识别截图。";
            return;
        }
        if (_lastExternalWindow == nint.Zero || !GetWindowRect(_lastExternalWindow, out var rect))
        {
            PerceptionFeedbackText.Text = "没有找到可以截取的最近活动窗口。";
            return;
        }
        var path = string.Empty;
        try
        {
            path = CaptureWindowToTemporaryFile(_lastExternalWindow, rect);
            ScreenshotConsentBox.IsChecked = false;
            await _pipe.SendAsync(new
            {
                type = "perception.image",
                request_id = Guid.NewGuid().ToString("N"),
                path,
                source = "once",
            });
            PerceptionFeedbackText.Text = "截图已临时保存并提交识别；不会自动连续截图。";
        }
        catch (Exception ex)
        {
            try
            {
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
                // 临时截图的清理由系统临时目录继续托管，不覆盖原始错误。
            }
            PerceptionFeedbackText.Text = ex.Message;
        }
    }

    private static string CaptureWindowToTemporaryFile(nint handle, NativeRect? knownRect = null)
    {
        if (handle == nint.Zero) throw new InvalidOperationException("没有找到可以截取的窗口");
        NativeRect rect;
        if (knownRect.HasValue)
        {
            rect = knownRect.Value;
        }
        else if (!GetWindowRect(handle, out rect))
        {
            throw new InvalidOperationException("没有找到可以截取的窗口");
        }
        var width = Math.Clamp(rect.Right - rect.Left, 1, 4096);
        var height = Math.Clamp(rect.Bottom - rect.Top, 1, 4096);
        var path = Path.Combine(Path.GetTempPath(), $"unnameko_capture_{Guid.NewGuid():N}.png");
        using var bitmap = new System.Drawing.Bitmap(width, height);
        using (var graphics = System.Drawing.Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(rect.Left, rect.Top, 0, 0, new System.Drawing.Size(width, height));
        }
        bitmap.Save(path, System.Drawing.Imaging.ImageFormat.Png);
        return path;
    }

    private async void StartObservationButton_Click(object sender, RoutedEventArgs e)
    {
        if (!CanReadFromButton(ObservationConsentBox.IsChecked == true, "限时观察"))
        {
            ObservationStatusText.Text = PerceptionFeedbackText.Text;
            return;
        }
        var handle = _lastExternalWindow;
        if (handle == nint.Zero || !DesktopPerceptionService.IsWindow(handle))
        {
            ObservationStatusText.Text = "请先切换到想观察的窗口，再回到这里开始。";
            return;
        }
        var selected = ObservationDurationBox.SelectedItem as System.Windows.Controls.ComboBoxItem;
        var minutes = int.TryParse(selected?.Tag?.ToString(), out var parsed) ? parsed : 5;
        _observationWindow = handle;
        _observationEndsAt = DateTimeOffset.Now.AddMinutes(Math.Clamp(minutes, 1, 60));
        _lastObservationContextAt = DateTimeOffset.MinValue;
        _lastObservationVisionAt = DateTimeOffset.MinValue;
        _lastObservationStructureFingerprint = string.Empty;
        _lastObservationVisualSample = null;
        _observationVisionCount = 0;
        _observationVisionInFlight = false;
        StartObservationButton.IsEnabled = false;
        StopObservationButton.IsEnabled = true;
        ObservationDurationBox.IsEnabled = false;
        ObservationConsentBox.IsEnabled = false;
        ObservationVisionConsentBox.IsEnabled = false;
        PerceptionIndicator.Visibility = Visibility.Visible;
        _observationTimer.Start();
        ShowToast("限时窗口观察已经开始。", "#EFF8F1");
        await ObserveWindowTickAsync(forceContext: true);
    }

    private void StopObservationButton_Click(object sender, RoutedEventArgs e) =>
        StopObservation("主人已手动停止观察");

    private void StopObservation(string reason)
    {
        if (_observationWindow == nint.Zero && !_observationTimer.IsEnabled) return;
        _observationTimer.Stop();
        _observationWindow = nint.Zero;
        _lastObservationVisualSample = null;
        _lastObservationStructureFingerprint = string.Empty;
        _observationVisionInFlight = false;
        StartObservationButton.IsEnabled = true;
        StopObservationButton.IsEnabled = false;
        ObservationDurationBox.IsEnabled = true;
        ObservationConsentBox.IsEnabled = true;
        ObservationVisionConsentBox.IsEnabled = true;
        ObservationConsentBox.IsChecked = false;
        ObservationVisionConsentBox.IsChecked = false;
        UpdatePerceptionIndicator();
        ObservationStatusText.Text = reason;
        PerceptionFeedbackText.Text = $"{reason}；已经取得的结果仍保留在预览中。";
    }

    private async Task ObserveWindowTickAsync(bool forceContext = false)
    {
        if (_observationTickBusy || _observationWindow == nint.Zero) return;
        if (DateTimeOffset.Now >= _observationEndsAt)
        {
            StopObservation("限时观察已到期");
            return;
        }
        if (!DesktopPerceptionService.IsWindow(_observationWindow))
        {
            StopObservation("目标窗口已经关闭，观察自动结束");
            return;
        }

        _observationTickBusy = true;
        try
        {
            var handle = _observationWindow;
            var structureTask = Task.Run(() => _perception.CaptureStructure(handle));
            var visualTask = Task.Run(() => _perception.CaptureVisualSample(handle));
            await Task.WhenAll(structureTask, visualTask);
            var snapshot = structureTask.Result;
            var visual = visualTask.Result;
            var visualDifference = DesktopPerceptionService.Difference(_lastObservationVisualSample, visual);
            var structureChanged = !string.Equals(_lastObservationStructureFingerprint, snapshot.Fingerprint,
                StringComparison.Ordinal);
            var changed = forceContext || structureChanged || visualDifference >= 0.08;
            _lastObservationStructureFingerprint = snapshot.Fingerprint;
            _lastObservationVisualSample = visual;

            var remaining = _observationEndsAt - DateTimeOffset.Now;
            var remainingText = $"{Math.Max(0, (int)Math.Ceiling(remaining.TotalMinutes))} 分钟";
            PerceptionIndicatorText.Text = $"● 观察中 {remainingText}";
            ObservationStatusText.Text = $"正在观察：{snapshot.Title} · 剩余约 {remainingText} · 视觉分析 {_observationVisionCount}/5";

            if (changed && (forceContext || DateTimeOffset.Now - _lastObservationContextAt >= TimeSpan.FromSeconds(8)))
            {
                var prefix = forceContext ? "【陪伴观察开始】" : $"【观察到窗口变化 {DateTime.Now:HH:mm:ss}】";
                AddPerceptionContext($"{prefix}\n{snapshot.ToContext(forceContext ? 50 : 24)}");
                _lastObservationContextAt = DateTimeOffset.Now;
            }

            if (!forceContext && visualDifference >= 0.10 && ObservationVisionConsentBox.IsChecked == true &&
                _connected && !_observationVisionInFlight && _observationVisionCount < 5 &&
                DateTimeOffset.Now - _lastObservationVisionAt >= TimeSpan.FromSeconds(30))
            {
                await SubmitObservationScreenshotAsync(handle);
            }
        }
        catch (Exception ex)
        {
            ObservationStatusText.Text = $"本次采样失败：{ex.Message}";
        }
        finally
        {
            _observationTickBusy = false;
        }
    }

    private async Task SubmitObservationScreenshotAsync(nint handle)
    {
        var path = string.Empty;
        try
        {
            path = CaptureWindowToTemporaryFile(handle);
            _observationVisionInFlight = true;
            _observationVisionCount++;
            _lastObservationVisionAt = DateTimeOffset.Now;
            await _pipe.SendAsync(new
            {
                type = "perception.image",
                request_id = Guid.NewGuid().ToString("N"),
                path,
                source = "observation",
            });
        }
        catch
        {
            _observationVisionInFlight = false;
            if (!string.IsNullOrWhiteSpace(path))
            {
                try { File.Delete(path); } catch { }
            }
            throw;
        }
    }

    private void ClearPerceptionButton_Click(object sender, RoutedEventArgs e)
    {
        ClearPerceptionContext();
        PerceptionFeedbackText.Text = "待发送上下文已经清除。";
    }

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
        if (!_petMode) InputBox.Focus();
    }

    private async void ExitWindow()
    {
        _exitRequested = true;
        _windowTracker.Stop();
        StopObservation("窗口退出，观察已停止");
        _bubbleTimer.Stop();
        _toastTimer.Stop();
        _blinkTimer.Stop();
        _mouthTimer.Stop();
        _presenceTimer.Stop();
        _tray.Visible = false;
        _tray.Dispose();
        await _pipe.DisposeAsync();
        Close();
        System.Windows.Application.Current.Shutdown();
    }
}
